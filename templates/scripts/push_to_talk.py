"""Push-to-talk two-language voice input for Claude-Tutor.

Supports a TARGET language (the one being learned) and a COMMON language
(your native/communication tongue). You pick the language by which key you
hold — there is NO language auto-detection, so mixed-language speech (e.g.
Dutch words inside a Russian sentence) is transcribed in the language you
intended rather than guessed.

Loops forever:
  1. Wait for one of two hotkeys: the target key (default F9) or the common
     key (default F10).
  2. While the key is held, capture mono 16 kHz audio from the default mic.
  3. On release, transcribe via a resident whisper-server — started once on
     launch and kept warm in VRAM, so repeat transcriptions take well under a
     second — forcing the language bound to the key you held.
  4. If you held the target key, append an IPA pronunciation line; if you held
     the common key, keep plain text.
  5. Save the recording as recordings/rec_{lang}_{NNNN:04d}.wav.
  6. Overwrite recordings/latest_transcript.txt with the payload.

The companion UserPromptSubmit hook (inject_transcript.py) reads
latest_transcript.txt on every Enter you press in Claude Code and
prepends it as context to your next prompt.

Usage (from a separate terminal, leave it running):
    py push_to_talk.py --target nl --common ru
    py push_to_talk.py --target nl --common ru --target-hotkey f9 --common-hotkey f10
    py push_to_talk.py --target nl --common ru --model D:\\...\\ggml-small-q5_1.bin

Press Ctrl+C in this terminal to stop.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# IPA contains non-ASCII characters (ˈ, ɪ, ŋ, …). Force UTF-8 on stdout/stderr
# so printing the transcript in PowerShell (default cp1252) doesn't crash.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# Third-party deps — print an actionable hint instead of a raw traceback
# if the user is running this with a Python that doesn't have them installed.
_REQUIRED = ("numpy", "sounddevice", "scipy", "pynput", "pywinauto", "pyperclip")
try:
    import numpy as np
    import sounddevice as sd
    from pynput import keyboard
    from scipy.io.wavfile import write as wav_write
    import pyperclip
    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys
except ImportError as _exc:
    missing = _exc.name or "a required package"
    print(
        f"ERROR: missing dependency '{missing}' for this Python interpreter.\n"
        f"       Interpreter : {sys.executable}\n"
        f"       Install with: py -m pip install --user {' '.join(_REQUIRED)}\n"
        f"\n"
        f"If you have multiple Pythons installed, make sure the 'py' you used\n"
        f"to launch this script is the same one you install into.",
        file=sys.stderr,
    )
    sys.exit(2)

import ctypes  # noqa: E402
import os       # noqa: E402
import time     # noqa: E402  (kept after the dep-check block for clearer error UX)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
LOG_PATH = PROJECT_ROOT / "logs" / "push_to_talk.log"
LATEST_TRANSCRIPT = RECORDINGS_DIR / "latest_transcript.txt"

SAMPLE_RATE = 16000  # whisper.cpp expects 16 kHz mono
WHISPER_DIR = PROJECT_ROOT / "tools" / "whisper.cpp"
DEFAULT_WHISPER_SERVER = WHISPER_DIR / "bin" / "Release" / "whisper-server.exe"
DEFAULT_MODEL = WHISPER_DIR / "models" / "ggml-medium-q5_0.bin"

# A resident whisper-server keeps the model + CUDA context warm in VRAM so each
# transcription is just an HTTP round-trip (~0.3 s) instead of a per-clip cold
# start (~3.5 s with the medium model). Bound to localhost only.
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8910          # avoids whisper-server's own default of 8080
SERVER_LOG_PATH = PROJECT_ROOT / "logs" / "whisper_server.log"

ESPEAK_DIR = PROJECT_ROOT / "tools" / "espeak-ng"
DEFAULT_ESPEAK_NG = ESPEAK_DIR / "espeak-ng.exe"
DEFAULT_ESPEAK_DATA = ESPEAK_DIR / "espeak-ng-data"

DEFAULT_TARGET_HOTKEY = "f9"   # hold to speak the TARGET language (+IPA)
DEFAULT_COMMON_HOTKEY = "f10"  # hold to speak the COMMON language (text only)
DEFAULT_WINDOW_TITLE_RE = r".*Claude.*"

# Map our ISO 639-1 codes to espeak-ng voice names. Codes not listed here
# fall through to the lang code as-is (espeak-ng accepts many of them
# verbatim — e.g. "nl", "de", "fr", "ru" — but English needs en-us to
# avoid defaulting to en-gb).
LANG_TO_ESPEAK_VOICE: dict[str, str] = {
    "en": "en-us",
    "zh": "cmn",
}


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def resolve_hotkey(name: str) -> keyboard.Key | keyboard.KeyCode:
    """Translate a string like 'f9' or 'space' into a pynput Key constant."""
    key = name.strip().lower()
    if hasattr(keyboard.Key, key):
        return getattr(keyboard.Key, key)
    if len(key) == 1:
        return keyboard.KeyCode.from_char(key)
    raise ValueError(f"unknown hotkey: {name!r}")


def format_device_list() -> str:
    """Return a human-readable listing of input and output audio devices."""
    lines = ["Input devices (microphones — for push-to-talk):"]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            host = sd.query_hostapis(d["hostapi"])["name"]
            lines.append(f"  [{i}] {d['name']}  (in={d['max_input_channels']}, {host})")
    lines.append("Output devices (speakers/headphones — for TTS playback):")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            host = sd.query_hostapis(d["hostapi"])["name"]
            lines.append(f"  [{i}] {d['name']}  (out={d['max_output_channels']}, {host})")
    return "\n".join(lines)


# When a device name matches multiple endpoints (the same hardware is exposed
# once per Windows host API), prefer modern reliable APIs. PortAudio's MME
# wrapper truncates names to 31 chars and can silently route Bluetooth audio
# to nowhere; WASAPI is the modern endpoint and works for BT/USB/HDMI alike.
_HOST_API_PREFERENCE = ("wasapi", "directsound", "mme", "wdmks")


def _host_api_rank(name: str) -> int:
    n = name.lower().replace("-", "").replace(" ", "")
    for i, key in enumerate(_HOST_API_PREFERENCE):
        if key in n:
            return i
    return len(_HOST_API_PREFERENCE)


def resolve_audio_device(spec: str | None, want_input: bool) -> int | None:
    """Resolve a device spec to a sounddevice device index.

    `spec` may be a device index ("9") or a case-insensitive substring of the
    device name ("USB PnP"). Names are preferred in practice because device
    *indices* are not stable across reboots or replugs, while the name is.

    Returns None when `spec` is empty (caller should use the system default).
    Raises ValueError when an index is invalid or a name matches nothing.
    When a name matches several devices (the same hardware is usually exposed
    once per host API — MME, DirectSound, WASAPI, …) the most reliable host
    API wins (WASAPI > DirectSound > MME > WDM-KS), and the alternatives are
    logged.
    """
    if not spec:
        return None
    spec = str(spec).strip()
    devices = sd.query_devices()
    chan_key = "max_input_channels" if want_input else "max_output_channels"
    kind = "input" if want_input else "output"

    if spec.isdigit():
        idx = int(spec)
        if idx < 0 or idx >= len(devices):
            raise ValueError(f"device index {idx} out of range (0..{len(devices) - 1})")
        if devices[idx][chan_key] <= 0:
            raise ValueError(f"device [{idx}] {devices[idx]['name']!r} has no {kind} channels")
        return idx

    needle = spec.lower()
    matches = [
        i for i, d in enumerate(devices)
        if d[chan_key] > 0 and needle in d["name"].lower()
    ]
    if not matches:
        raise ValueError(f"no {kind} device name contains {spec!r} (try --list-devices)")
    if len(matches) > 1:
        matches.sort(key=lambda i: (
            _host_api_rank(sd.query_hostapis(devices[i]["hostapi"])["name"]),
            i,
        ))
        alts = ", ".join(
            f"[{i}] {devices[i]['name']} ({sd.query_hostapis(devices[i]['hostapi'])['name']})"
            for i in matches
        )
        chosen = matches[0]
        logging.info(
            "%s device %r matched several; picked [%d] %s (%s). Candidates: %s",
            kind, spec, chosen, devices[chosen]["name"],
            sd.query_hostapis(devices[chosen]["hostapi"])["name"], alts,
        )
    return matches[0]


def next_sequence_number(lang: str) -> int:
    """Find the next NNNN for rec_{lang}_NNNN.wav in RECORDINGS_DIR."""
    pattern = re.compile(rf"^rec_{re.escape(lang)}_(\d+)\.wav$", re.IGNORECASE)
    highest = 0
    if RECORDINGS_DIR.exists():
        for entry in RECORDINGS_DIR.iterdir():
            match = pattern.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def record_until_release(hotkeys: dict, input_device: int | None = None) -> tuple[np.ndarray | None, str | None]:
    """Wait until one of several hotkeys is pressed, capture audio while it is
    held, then return (audio_int16, label) where `label` is the value mapped to
    the pressed key in `hotkeys` (here, the forced language code).

    `hotkeys` maps a pynput Key/KeyCode -> label string. Only the first key
    pressed is honored until it is released, so holding two at once is safe.
    `input_device` is a sounddevice device index, or None for the system
    default microphone.
    Returns (None, label) if the key was tapped without producing audio.
    """
    chunks: list[np.ndarray] = []
    recording = threading.Event()
    stop_signal = threading.Event()
    pressed: dict[str, object] = {"key": None}

    def audio_cb(indata, frames, time_info, status):  # noqa: ANN001
        if recording.is_set():
            chunks.append(indata.copy())

    def on_press(key):
        if key in hotkeys and not recording.is_set():
            pressed["key"] = key
            recording.set()
            print(f"  [recording '{hotkeys[key]}'...]", flush=True)

    def on_release(key):
        if recording.is_set() and key == pressed["key"]:
            recording.clear()
            stop_signal.set()
            return False  # stop the listener

    # WASAPI shared-mode rejects rates that differ from the endpoint's
    # configured rate (most USB/BT mics expose 44.1 or 48 kHz, not 16 kHz that
    # whisper.cpp wants). Ask Windows to resample for us. Other host APIs
    # (DirectSound, MME) resample at the system mixer and accept any rate.
    extra_settings = None
    if input_device is not None:
        host_api_name = sd.query_hostapis(sd.query_devices(input_device)["hostapi"])["name"].lower()
        if "wasapi" in host_api_name:
            extra_settings = sd.WasapiSettings(auto_convert=True)

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16",
        device=input_device, callback=audio_cb,
        extra_settings=extra_settings,
    ):
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        stop_signal.wait()
        listener.stop()

    label = hotkeys.get(pressed["key"])
    if not chunks:
        return None, label
    return np.concatenate(chunks, axis=0), label


def format_hotkey_name(hotkey: keyboard.Key | keyboard.KeyCode) -> str:
    if isinstance(hotkey, keyboard.Key):
        return hotkey.name.upper()
    return repr(hotkey.char) if hotkey.char else repr(hotkey)


def parse_whisper_json(json_text: str) -> tuple[str, str]:
    """Extract (transcription_text, detected_lang_code) from whisper verbose JSON.

    whisper.cpp's verbose JSON (server response_format=verbose_json, or the CLI's
    -oj) looks like:
        {"result": {"language": "ru"},
         "transcription": [{"text": " ..."}, ...]}
    Returns ("", "") if the structure is missing/unparseable.
    """
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return "", ""
    lang = ((data.get("result") or {}).get("language") or "").strip()
    segments = data.get("transcription") or []
    text = " ".join(
        (seg.get("text") or "").strip()
        for seg in segments
        if isinstance(seg, dict)
    ).strip()
    return text, lang


# ---------------------------------------------------------------------------
# Resident whisper-server backend. Keeping one server process alive avoids
# reloading the model + re-initialising the CUDA context on every clip, which
# is what made the old per-call whisper-cli path slow (~3.5 s vs <1 s here).
# ---------------------------------------------------------------------------

def find_free_port(host: str, preferred: int, attempts: int = 20) -> int | None:
    """Return the first bindable TCP port at or after `preferred` on `host`.

    The resident whisper-server hard-fails if its port is already taken — by
    another app, or by another project's claude-speech server sharing the
    default 8910. Rather than exiting with an error and making the user pass
    --server-port by hand, probe upward from the requested port and use the
    first free one. Returns None if none of `attempts` ports are free.

    Note: we deliberately do NOT set SO_REUSEADDR. On Windows it lets a bind
    succeed on a port that's already in use, which would defeat the probe;
    a plain bind that raises OSError is the reliable "port is taken" signal.
    """
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
                return port
            except OSError:
                continue
    return None


def start_whisper_server(
    server_bin: Path, model: Path, host: str, port: int
) -> subprocess.Popen | None:
    """Launch whisper-server detached, logging to logs/whisper_server.log.

    Returns the Popen handle, or None if the process couldn't be spawned. The
    server loads the model once and serves POST /inference for the daemon's
    lifetime; readiness is confirmed separately via wait_for_server().
    """
    SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(server_bin),
        "-m", str(model),
        "--host", host,
        "--port", str(port),
        "-nt",  # no timestamps in the transcription text
    ]
    logging.info("starting whisper-server: %s", " ".join(cmd))
    # CREATE_NO_WINDOW keeps the server from popping a console window when the
    # daemon itself is launched detached.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        log_fh = open(SERVER_LOG_PATH, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        logging.error("could not start whisper-server: %s", exc)
        return None
    return proc


def wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll the server until it accepts connections and the model is loaded.

    whisper-server opens its TCP port slightly before the model finishes
    loading, so we probe the HTTP endpoint (which only answers once the model
    is ready) rather than just the socket. Returns True once ready.
    """
    deadline = time.time() + timeout
    url = f"http://{host}:{port}/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0):
                return True
        except urllib.error.HTTPError:
            # Any HTTP status means the server is up and answering.
            return True
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def transcribe_via_server(
    wav_path: Path, lang: str, host: str, port: int
) -> str:
    """Transcribe a clip through the resident whisper-server.

    POSTs the WAV as multipart/form-data to /inference, forcing the language.
    Returns the transcription text, or "" on any error (the caller skips the
    clip and logs). Built with stdlib only (no requests dependency).
    """
    try:
        audio = wav_path.read_bytes()
    except OSError as exc:
        logging.error("could not read capture for server transcription: %s", exc)
        return ""

    boundary = uuid.uuid4().hex
    pre = []
    pre.append(f"--{boundary}\r\n")
    pre.append(
        'Content-Disposition: form-data; name="file"; '
        f'filename="{wav_path.name}"\r\n'
    )
    pre.append("Content-Type: audio/wav\r\n\r\n")
    fields = {"language": lang, "response_format": "json", "temperature": "0"}
    post = ["\r\n"]
    for key, val in fields.items():
        post.append(f"--{boundary}\r\n")
        post.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n')
        post.append(f"{val}\r\n")
    post.append(f"--{boundary}--\r\n")
    body = "".join(pre).encode("utf-8") + audio + "".join(post).encode("utf-8")

    req = urllib.request.Request(
        f"http://{host}:{port}/inference",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        logging.error("whisper-server request failed: %s", exc)
        return ""

    # /inference with response_format=json returns {"text": "..."}; some builds
    # return verbose JSON with a "transcription" list. Handle both, then fall
    # back to the raw body.
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw.strip()
    if isinstance(data, dict):
        if isinstance(data.get("text"), str):
            return data["text"].strip()
        text, _ = parse_whisper_json(raw)
        if text:
            return text
    return raw.strip()


def stop_whisper_server(proc: subprocess.Popen | None) -> None:
    """Terminate the resident server so it doesn't keep holding VRAM."""
    if proc is None or proc.poll() is not None:
        return
    logging.info("stopping whisper-server (pid=%s)", proc.pid)
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError as exc:
        logging.warning("error stopping whisper-server: %s", exc)


def kill_stale_whisper_servers(server_bin: Path) -> None:
    """Kill orphaned whisper-server processes for THIS project before starting.

    A daemon that was force-killed (e.g. via `/claude-speech off`) leaves its
    server child running and holding VRAM. We match only servers whose command
    line references this project's whisper.cpp tools dir, so other projects'
    servers are left alone.
    """
    # PowerShell single-quoted strings take backslashes literally (only '' needs
    # escaping), so the path goes in as-is; we just guard against stray quotes.
    needle = str(server_bin.parent).lower().replace("'", "''")
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'whisper-server.exe' -and "
        f"$_.CommandLine -and $_.CommandLine.ToLower().Contains('{needle}') }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.warning("could not sweep stale whisper-server processes: %s", exc)


def to_ipa(
    text: str, voice: str, espeak_ng: Path, espeak_data: Path
) -> str:
    """Run espeak-ng to convert orthographic text into an IPA string.

    Returns the raw IPA string (no surrounding brackets). On error, returns
    an empty string and logs.
    """
    if not text.strip():
        return ""
    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(espeak_data)
    try:
        result = subprocess.run(
            [str(espeak_ng), "-v", voice, "--ipa", "-q", text],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.error("espeak-ng invocation failed: %s", exc)
        return ""
    if result.returncode != 0:
        logging.error("espeak-ng failed (rc=%d):\n%s", result.returncode, result.stderr)
        return ""
    # espeak-ng's --ipa output can include leading/trailing whitespace and
    # newlines between sentences. Collapse to a single line.
    return " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())


def write_latest_transcript(text: str, wav_path: Path, lang: str) -> None:
    """Overwrite latest_transcript.txt with just the transcription text.

    No metadata header — lang/wav info is already in the daemon's terminal
    output and log file. The transcript file is consumed by the
    UserPromptSubmit hook, which only cares about the spoken words.
    """
    LATEST_TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    LATEST_TRANSCRIPT.write_text(text + "\n", encoding="utf-8")


def find_chat_input(win, retries: int = 4, retry_delay: float = 0.2) -> object | None:
    """Locate the chat input control inside the Claude app window via UIA.

    The Claude desktop app is Electron and exposes its whole web view as a
    single UIA ``Document`` node, so the ``<textarea>`` is NOT a distinct
    ``Edit`` control we can match by type. The input is, however, reachable
    as a focusable ``Group`` in the bottom strip of the window spanning most
    of its width — the other focusable Groups down there are narrow toolbar
    buttons. We pick the single Group matching that geometric signature; if
    zero or more than one match we return None and let the caller fall back
    to plain window-level focus.

    Cold-start retry: the first UIA tree walk right after the window is
    brought to the foreground often returns before Chromium has realized the
    web view's accessibility tree, so the input Group isn't present yet and
    zero candidates are found. That is why the very first transcript of a
    session used to silently miss the input box and only the second attempt
    landed. When zero candidates are found we wait briefly and re-walk the
    tree up to ``retries`` times. An *ambiguous* (>1) result is a different
    problem that retrying won't fix, so we bail immediately in that case.

    Returns the matched pywinauto element, or ``None`` when no unambiguous
    candidate was found.
    """
    try:
        wr = win.rectangle()
    except Exception as exc:
        logging.warning("could not get window rect for input search: %s", exc)
        return None

    # Lower ~28% of the window height is where the input row lives; the input
    # spans most of the column width while toolbar buttons are narrow, so a
    # >=40%-of-window-width filter isolates the input.
    bottom_threshold = wr.top + int((wr.bottom - wr.top) * 0.72)
    min_width = int((wr.right - wr.left) * 0.40)

    for attempt in range(retries):
        try:
            groups = win.descendants(control_type="Group")
        except Exception as exc:
            logging.warning("UIA descendants(Group) failed: %s", exc)
            return None

        candidates = []
        for g in groups:
            try:
                r = g.rectangle()
                focusable = bool(g.element_info.element.CurrentIsKeyboardFocusable)
            except Exception:
                continue
            if focusable and r.top >= bottom_threshold and (r.right - r.left) >= min_width:
                candidates.append(g)

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logging.info(
                "UIA chat-input search found %d focusable wide Groups; ambiguous, skipping",
                len(candidates),
            )
            return None
        # Zero candidates — the accessibility tree is probably still warming
        # up. Wait and re-walk, unless this was the last attempt.
        if attempt < retries - 1:
            logging.info(
                "UIA chat-input search found no candidate (attempt %d/%d); retrying",
                attempt + 1, retries,
            )
            time.sleep(retry_delay)

    return None


def focus_chat_input(win) -> bool:
    """Drop keyboard focus straight into the chat input box via UIA.

    Calls ``set_focus`` on the located input Group — no mouse click, so the
    user's cursor is left undisturbed. In the Claude desktop app this does
    land the caret inside the inner textarea (verified empirically), so a
    subsequent paste goes to the right place even if the caret had been
    elsewhere (a button, the sidebar) beforehand.

    Best-effort. Returns True on success, False if the input couldn't be
    identified unambiguously or ``set_focus`` raised; the caller then falls
    back to whatever control the window already has focused.
    """
    element = find_chat_input(win)
    if element is None:
        return False
    try:
        element.set_focus()
    except Exception as exc:
        logging.warning("set_focus on chat input Group failed: %s", exc)
        return False
    return True


def paste_from_clipboard() -> None:
    """Trigger a clipboard paste via a low-level Win32 Ctrl+V (keybd_event).

    pywinauto's ``send_keys("^v")`` sends the Ctrl modifier in a way the
    Claude desktop app (Electron/Chromium) silently ignores — plain-text
    keystroke routing works, but the synthetic Ctrl+V never registers as a
    paste, so nothing lands in the input. Driving the key transitions
    through the Win32 ``keybd_event`` API directly DOES register as paste.
    (Shift+Insert also works in this app and is an equally valid fallback.)
    """
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def submit_to_claude_code(text: str, window_title_re: str, press_enter: bool = True) -> bool:
    """Find the Claude Code window, paste the text into it, and (optionally)
    press Enter.

    When `press_enter` is False the text is pasted into the chat input but NOT
    submitted, so you can review/edit it and send manually.

    Windows 11's anti-focus-stealing protection routinely blocks
    pywinauto's set_focus() — silently! — when called from a process the
    user didn't recently interact with. Without the Alt-tap workaround
    below, keystrokes would go to whatever window currently has focus
    (typically the daemon's own terminal). After the Alt tap, we verify
    via GetForegroundWindow that focus actually moved; if not, we abort
    rather than spray keystrokes into the wrong window. The caller keeps
    latest_transcript.txt so the UserPromptSubmit hook fallback can still
    inject on manual Enter.
    """
    try:
        windows = Desktop(backend="uia").windows(title_re=window_title_re)
    except Exception as exc:
        logging.warning("window enumeration failed: %s", exc)
        return False

    if not windows:
        logging.warning("no window matching %r found", window_title_re)
        return False

    win = windows[0]
    target_handle = int(win.handle)
    logging.info("found window: %r (handle=%s)", win.window_text(), target_handle)

    # Save the user's clipboard so we don't clobber whatever they had on it.
    saved_clip: str | None
    try:
        saved_clip = pyperclip.paste()
    except Exception:
        saved_clip = None

    try:
        pyperclip.copy(text)
    except Exception as exc:
        logging.warning("clipboard copy failed: %s", exc)
        if saved_clip is not None:
            try: pyperclip.copy(saved_clip)
            except Exception: pass
        return False

    # Windows 11 anti-focus-stealing trick: tap Alt to release the
    # foreground-window lock so SetForegroundWindow() will be honored
    # for the very next call. Without this, set_focus() silently no-ops
    # and keystrokes go to the currently-foreground window.
    try:
        send_keys("%")  # tap-and-release Alt
        time.sleep(0.05)
    except Exception as exc:
        logging.warning("alt-tap (focus-unlock) failed: %s", exc)

    try:
        win.set_focus()
    except Exception as exc:
        logging.warning("set_focus failed: %s", exc)
    time.sleep(0.20)  # let focus settle

    # Verify focus actually landed on the target window. If not, the
    # keystrokes would be sent to the wrong window — bail out.
    user32 = ctypes.windll.user32
    fg_handle = int(user32.GetForegroundWindow())
    if fg_handle != target_handle:
        logging.warning(
            "foreground window is not target (target=%s, fg=%s) — aborting auto-submit, fallback hook will inject",
            target_handle, fg_handle,
        )
        if saved_clip is not None:
            try: pyperclip.copy(saved_clip)
            except Exception: pass
        return False

    # Window is foreground — now drop the caret straight into the chat input
    # box via UIA so the paste lands there even if the caret had been on a
    # button or the sidebar. Best-effort: on failure we fall through to
    # whatever control the window already has focused (then the caret must
    # already be in the input — see the startup NOTE).
    if focus_chat_input(win):
        logging.info("focused chat input via UIA Group")
        time.sleep(0.10)  # let focus settle before we paste

    try:
        paste_from_clipboard()  # Ctrl+V (low-level) → paste transcript into chat input
        if press_enter:
            time.sleep(0.10)
            send_keys("{ENTER}")   # submit
    except Exception as exc:
        logging.exception("keystroke send failed: %s", exc)
        return False
    finally:
        # Restore the user's prior clipboard. Tiny delay so the paste has
        # actually consumed the clipboard before we overwrite it.
        time.sleep(0.05)
        if saved_clip is not None:
            try:
                pyperclip.copy(saved_clip)
            except Exception:
                pass

    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Push-to-talk voice input for Claude-Tutor")
    parser.add_argument("--target", help="ISO 639-1 code of the language being learned (spoken + IPA), e.g. nl, en, de")
    parser.add_argument("--common", help="ISO 639-1 code of your communication language (notes, never spoken), e.g. ru")
    parser.add_argument("--lang", help=argparse.SUPPRESS)  # back-compat alias for --target
    parser.add_argument("--target-hotkey", default=DEFAULT_TARGET_HOTKEY, help="hold to speak the TARGET language, forced transcription + IPA (default: f9)")
    parser.add_argument("--common-hotkey", default=DEFAULT_COMMON_HOTKEY, help="hold to speak the COMMON language, forced transcription, no IPA (default: f10)")
    parser.add_argument("--hotkey", help=argparse.SUPPRESS)  # back-compat alias for --target-hotkey
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="path to ggml whisper model")
    parser.add_argument("--whisper-server", type=Path, default=DEFAULT_WHISPER_SERVER, help="path to whisper-server.exe (resident transcription backend)")
    parser.add_argument("--server-host", default=DEFAULT_SERVER_HOST, help=f"host for the resident whisper-server (default: {DEFAULT_SERVER_HOST})")
    parser.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT, help=f"port for the resident whisper-server (default: {DEFAULT_SERVER_PORT})")
    parser.add_argument("--espeak-ng", type=Path, default=DEFAULT_ESPEAK_NG, help="path to espeak-ng.exe (for IPA conversion)")
    parser.add_argument("--espeak-data", type=Path, default=DEFAULT_ESPEAK_DATA, help="path to espeak-ng-data dir")
    parser.add_argument(
        "--espeak-voice",
        default=None,
        help="override espeak-ng voice (default: derived from --lang, e.g. 'en' -> 'en-us')",
    )
    parser.add_argument(
        "--window-title-re",
        default=DEFAULT_WINDOW_TITLE_RE,
        help="regex matching the Claude Code window title (default: .*Claude.*)",
    )
    parser.add_argument(
        "--no-auto-submit",
        action="store_true",
        help="skip the auto-paste step entirely; the UserPromptSubmit hook will inject the transcript when you press Enter manually",
    )
    parser.add_argument(
        "--no-enter",
        action="store_true",
        help="paste the transcript into the chat window but do NOT press Enter, so you can review/edit and submit it yourself",
    )
    parser.add_argument(
        "--input-device",
        default=None,
        help="microphone to record from: device index or a substring of its name (default: system default). Use --list-devices to see options.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="print available audio input/output devices and exit",
    )
    parser.add_argument(
        "--list-windows",
        action="store_true",
        help="print all visible top-level window titles and exit (use to discover the right --window-title-re)",
    )
    args = parser.parse_args(argv)

    if args.list_devices:
        print(format_device_list())
        return 0

    if args.list_windows:
        print("All visible top-level windows:")
        for w in Desktop(backend="uia").windows():
            title = w.window_text()
            if title:
                print(f"  {title!r}")
        return 0

    setup_logging()

    target = (args.target or args.lang or "").strip().lower()
    common = (args.common or "").strip().lower()
    if not target:
        logging.error("no target language given; pass --target <code> (e.g. nl)")
        return 2
    if not common:
        logging.error("no common language given; pass --common <code> (e.g. ru)")
        return 2
    if target == common:
        logging.error("--target and --common must differ (both were %r)", target)
        return 2

    if not args.whisper_server.is_file():
        logging.error("whisper-server not found: %s", args.whisper_server)
        return 2
    if not args.model.is_file():
        logging.error("model not found: %s", args.model)
        return 2
    if not args.espeak_ng.is_file():
        logging.error("espeak-ng.exe not found: %s", args.espeak_ng)
        return 2
    if not args.espeak_data.is_dir():
        logging.error("espeak-ng-data dir not found: %s", args.espeak_data)
        return 2

    espeak_voice = args.espeak_voice or LANG_TO_ESPEAK_VOICE.get(target, target)

    try:
        target_key = resolve_hotkey(args.hotkey or args.target_hotkey)
        common_key = resolve_hotkey(args.common_hotkey)
    except ValueError as exc:
        logging.error(str(exc))
        return 2
    if target_key == common_key:
        logging.error("target and common hotkeys must differ (both resolve to %s)", format_hotkey_name(target_key))
        return 2

    try:
        input_device = resolve_audio_device(args.input_device, want_input=True)
    except ValueError as exc:
        logging.error("input device: %s", exc)
        return 2
    if input_device is not None:
        logging.info("recording from input device [%s] %s", input_device, sd.query_devices(input_device)["name"])

    # Map each hotkey to the language it forces. The pressed key, not language
    # detection, decides how the clip is transcribed — this avoids misreading
    # mixed-language speech (e.g. Dutch words inside a Russian sentence).
    hotkey_map = {target_key: target, common_key: common}

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    logging.info(
        "ready: target=%s (%s), common=%s (%s), espeak_voice=%s, model=%s, auto_submit=%s",
        target, format_hotkey_name(target_key), common, format_hotkey_name(common_key),
        espeak_voice, args.model.name, not args.no_auto_submit,
    )
    print("=" * 60, flush=True)
    print(f"Push-to-talk active.", flush=True)
    print(f"  Hold {format_hotkey_name(target_key)} to speak TARGET '{target}' (transcribed as {target} + IPA via espeak-ng voice '{espeak_voice}').", flush=True)
    print(f"  Hold {format_hotkey_name(common_key)} to speak COMMON '{common}' (transcribed as {common}, no IPA).", flush=True)
    mic_label = (f"[{input_device}] {sd.query_devices(input_device)['name']}"
                 if input_device is not None else "system default")
    print(f"  Microphone: {mic_label}.", flush=True)
    print("The key you hold forces the language — no auto-detection. Ctrl+C to quit.", flush=True)

    # Show which windows the auto-submit step will see, so a wrong match
    # is obvious upfront instead of surfacing as "Enter got sent somewhere else".
    if not args.no_auto_submit:
        try:
            matches = [
                w.window_text()
                for w in Desktop(backend="uia").windows(title_re=args.window_title_re)
                if w.window_text()
            ]
        except Exception as exc:
            matches = []
            logging.warning("window enumeration at startup failed: %s", exc)

        if not matches:
            print(
                f"WARNING: no window matches {args.window_title_re!r}. Auto-submit will fail.\n"
                f"         Run with --list-windows to see what's available, then pass --window-title-re '<regex>'.",
                flush=True,
            )
        else:
            print(f"Auto-submit target (matches for {args.window_title_re!r}):", flush=True)
            for i, t in enumerate(matches):
                marker = "  <- will use" if i == 0 else ""
                print(f"  {i}. {t!r}{marker}", flush=True)
            if len(matches) > 1:
                print(
                    f"  (multiple matches — pass --window-title-re '<more-specific-regex>' to disambiguate)",
                    flush=True,
                )
        print(
            "NOTE: the daemon focuses the chat input automatically (UIA).\n"
            "      If a transcript ever fails to appear, click the input box\n"
            "      once and try again — the auto-focus couldn't locate it.",
            flush=True,
        )
    else:
        print("Auto-submit DISABLED (--no-auto-submit). UserPromptSubmit hook will inject on manual Enter.", flush=True)
    print("=" * 60, flush=True)

    # Bring up the resident whisper-server. It is the only transcription backend,
    # so a failure to start is fatal (with an actionable message) rather than a
    # silent fallback.
    kill_stale_whisper_servers(args.whisper_server)
    # Pick a usable port: probe upward from the requested one and fall back to
    # the next free port if it's taken, instead of erroring out. If the probe
    # finds nothing free, keep the requested port so start-up fails loudly with
    # the actionable message below rather than silently.
    server_port = find_free_port(args.server_host, args.server_port) or args.server_port
    if server_port != args.server_port:
        logging.info("requested whisper-server port %d busy; using free port %d",
                     args.server_port, server_port)
        print(f"  note: port {args.server_port} is busy — using {server_port} instead.", flush=True)
    server_proc = start_whisper_server(
        args.whisper_server, args.model, args.server_host, server_port
    )
    if server_proc is None or not wait_for_server(args.server_host, server_port):
        logging.error("whisper-server failed to start on %s:%s", args.server_host, server_port)
        stop_whisper_server(server_proc)
        print(
            f"ERROR: whisper-server failed to start at {args.server_host}:{server_port}.\n"
            f"       See {SERVER_LOG_PATH} for details. Common causes: the port is\n"
            f"       already in use (try --server-port <other>) or the binary/model is\n"
            f"       missing. Provision binaries with `--gpu auto` (see README).",
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        f"  transcription: whisper-server (resident, warm) "
        f"at {args.server_host}:{server_port}.",
        flush=True,
    )
    print("=" * 60, flush=True)

    capture_path = RECORDINGS_DIR / "rec_capture.wav"
    try:
        while True:
            audio, lang = record_until_release(hotkey_map, input_device=input_device)
            if lang is None:
                continue  # a non-hotkey release slipped through; ignore
            if audio is None or len(audio) < SAMPLE_RATE // 4:  # <0.25s = ignore
                print("  [too short, ignored]\n", flush=True)
                continue

            wav_write(capture_path, SAMPLE_RATE, audio)
            duration = len(audio) / SAMPLE_RATE
            is_target = (lang == target)
            print(f"  captured {duration:.1f}s, transcribing forced '{lang}'...", flush=True)

            text = transcribe_via_server(capture_path, lang, args.server_host, server_port)
            if not text:
                print("  [empty transcript — transcription error, see log]\n", flush=True)
                continue

            # Persist the recording under the forced language code.
            seq = next_sequence_number(lang)
            wav_path = RECORDINGS_DIR / f"rec_{lang}_{seq:04d}.wav"
            try:
                if wav_path.exists():
                    wav_path.unlink()
                capture_path.replace(wav_path)
            except OSError as exc:
                logging.warning("could not rename capture to %s: %s", wav_path, exc)
                wav_path = capture_path
            print(f"  saved: {wav_path.name}", flush=True)

            # IPA is a pronunciation aid for the target language only.
            ipa = ""
            if is_target:
                ipa = to_ipa(text, espeak_voice, args.espeak_ng, args.espeak_data)
                if ipa:
                    payload = f"{text}\n[{ipa}]"
                else:
                    payload = text
                    logging.warning("empty IPA from espeak-ng for %r; falling back to text only", text)
            else:
                payload = text

            write_latest_transcript(payload, wav_path, lang)
            print(f"  text: {text}", flush=True)
            if is_target:
                print(f"  IPA : [{ipa}]" if ipa else "  IPA : (espeak-ng error — see log)", flush=True)
            else:
                print("  IPA : (skipped — common language)", flush=True)
            logging.info("transcribed %s: lang=%s text=%r ipa=%r", wav_path.name, lang, text, ipa)

            if args.no_auto_submit:
                print("  [auto-submit disabled; press Enter in Claude Code to inject]\n", flush=True)
            elif submit_to_claude_code(payload, args.window_title_re, press_enter=not args.no_enter):
                if args.no_enter:
                    # Pasted but not submitted — keep latest_transcript.txt as a
                    # fallback in case the paste landed in the wrong place.
                    print("  [pasted into Claude Code — review and press Enter to send]\n", flush=True)
                else:
                    # Auto-submit won — remove the file so the fallback hook
                    # doesn't re-inject the same content on the user's next Enter.
                    try:
                        LATEST_TRANSCRIPT.unlink()
                    except OSError:
                        pass
                    print("  [submitted to Claude Code]\n", flush=True)
            else:
                print(
                    "  [auto-submit failed; transcript kept for hook fallback — press Enter in Claude Code]\n",
                    flush=True,
                )

    except KeyboardInterrupt:
        print("\nbye", flush=True)
        return 0
    finally:
        stop_whisper_server(server_proc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
