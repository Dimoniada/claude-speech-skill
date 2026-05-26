"""Push-to-talk Dutch/English/etc. voice input for Claude-Tutor.

Loops forever:
  1. Wait for the configured hotkey to be pressed (default F9).
  2. While the hotkey is held, capture mono 16 kHz audio from the default mic.
  3. On release, save the recording as recordings/rec_{lang}_{NNNN:04d}.wav.
  4. Transcribe via whisper.cpp (whisper-cli) for the target language.
  5. Overwrite recordings/latest_transcript.txt with the transcribed text.

The companion UserPromptSubmit hook (inject_transcript.py) reads
latest_transcript.txt on every Enter you press in Claude Code and
prepends it as context to your next prompt.

Usage (from a separate terminal, leave it running):
    py push_to_talk.py --lang en
    py push_to_talk.py --lang nl --hotkey f10
    py push_to_talk.py --lang en --model D:\\Tools\\whisper.cpp\\models\\ggml-small-q5_1.bin

Press Ctrl+C in this terminal to stop.
"""
from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import threading
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

import os    # noqa: E402
import time  # noqa: E402  (kept after the dep-check block for clearer error UX)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
LOG_PATH = PROJECT_ROOT / "logs" / "push_to_talk.log"
LATEST_TRANSCRIPT = RECORDINGS_DIR / "latest_transcript.txt"

SAMPLE_RATE = 16000  # whisper.cpp expects 16 kHz mono
WHISPER_DIR = PROJECT_ROOT / "tools" / "whisper.cpp"
DEFAULT_WHISPER_CLI = WHISPER_DIR / "bin" / "Release" / "whisper-cli.exe"
DEFAULT_MODEL = WHISPER_DIR / "models" / "ggml-medium-q5_0.bin"

ESPEAK_DIR = PROJECT_ROOT / "tools" / "espeak-ng"
DEFAULT_ESPEAK_NG = ESPEAK_DIR / "espeak-ng.exe"
DEFAULT_ESPEAK_DATA = ESPEAK_DIR / "espeak-ng-data"

DEFAULT_HOTKEY = "f9"
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


def record_until_release(hotkey: keyboard.Key | keyboard.KeyCode) -> np.ndarray | None:
    """Block until the hotkey is pressed, capture audio while held, return the
    audio buffer as int16. Returns None if the hotkey was tapped without
    producing any audio (or release came before any sample arrived)."""
    chunks: list[np.ndarray] = []
    recording = threading.Event()
    stop_signal = threading.Event()

    def audio_cb(indata, frames, time_info, status):  # noqa: ANN001
        if recording.is_set():
            chunks.append(indata.copy())

    def on_press(key):
        if key == hotkey and not recording.is_set():
            recording.set()
            print(f"  [recording...]", flush=True)

    def on_release(key):
        if key == hotkey and recording.is_set():
            recording.clear()
            stop_signal.set()
            return False  # stop the listener

    print(f"Hold {format_hotkey_name(hotkey)} to record, release to transcribe.", flush=True)

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=audio_cb
    ):
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        stop_signal.wait()
        listener.stop()

    if not chunks:
        return None
    return np.concatenate(chunks, axis=0)


def format_hotkey_name(hotkey: keyboard.Key | keyboard.KeyCode) -> str:
    if isinstance(hotkey, keyboard.Key):
        return hotkey.name.upper()
    return repr(hotkey.char) if hotkey.char else repr(hotkey)


def run_whisper(
    wav_path: Path, lang: str, model: Path, whisper_cli: Path
) -> str:
    """Invoke whisper-cli, return the transcribed text (stripped)."""
    cmd = [
        str(whisper_cli),
        "-m", str(model),
        "-f", str(wav_path),
        "-l", lang,
        "-nt",           # no timestamps in output
        "-np",           # no progress prints
    ]
    logging.info("running whisper-cli: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        logging.error("whisper-cli failed (rc=%d):\n%s", result.returncode, result.stderr)
        return ""
    return result.stdout.strip()


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


def submit_to_claude_code(text: str, window_title_re: str) -> bool:
    """Find the Claude Code window, paste the text into it, press Enter.

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
    import ctypes
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

    try:
        send_keys("^v")        # Ctrl+V: paste the transcript into chat input
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
    parser.add_argument("--lang", default="en", help="ISO 639-1 code, e.g. en, nl, de")
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help="hotkey to hold while speaking (default: f9)")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="path to ggml whisper model")
    parser.add_argument("--whisper-cli", type=Path, default=DEFAULT_WHISPER_CLI, help="path to whisper-cli.exe")
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
        help="skip the auto-paste-and-Enter step; the UserPromptSubmit hook will inject the transcript when you press Enter manually",
    )
    parser.add_argument(
        "--list-windows",
        action="store_true",
        help="print all visible top-level window titles and exit (use to discover the right --window-title-re)",
    )
    args = parser.parse_args(argv)

    if args.list_windows:
        print("All visible top-level windows:")
        for w in Desktop(backend="uia").windows():
            title = w.window_text()
            if title:
                print(f"  {title!r}")
        return 0

    setup_logging()

    if not args.whisper_cli.is_file():
        logging.error("whisper-cli not found: %s", args.whisper_cli)
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

    espeak_voice = args.espeak_voice or LANG_TO_ESPEAK_VOICE.get(args.lang, args.lang)

    try:
        hotkey = resolve_hotkey(args.hotkey)
    except ValueError as exc:
        logging.error(str(exc))
        return 2

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    logging.info(
        "ready: lang=%s, espeak_voice=%s, hotkey=%s, model=%s, auto_submit=%s",
        args.lang, espeak_voice, format_hotkey_name(hotkey), args.model.name, not args.no_auto_submit,
    )
    print("=" * 60, flush=True)
    print(f"Push-to-talk active for language '{args.lang}' (IPA via espeak-ng voice '{espeak_voice}').", flush=True)
    print(f"Hold {format_hotkey_name(hotkey)} anywhere to record. Ctrl+C to quit.", flush=True)

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
    else:
        print("Auto-submit DISABLED (--no-auto-submit). UserPromptSubmit hook will inject on manual Enter.", flush=True)
    print("=" * 60, flush=True)

    try:
        while True:
            audio = record_until_release(hotkey)
            if audio is None or len(audio) < SAMPLE_RATE // 4:  # <0.25s = ignore
                print("  [too short, ignored]\n", flush=True)
                continue

            seq = next_sequence_number(args.lang)
            wav_path = RECORDINGS_DIR / f"rec_{args.lang}_{seq:04d}.wav"
            wav_write(wav_path, SAMPLE_RATE, audio)
            duration = len(audio) / SAMPLE_RATE
            print(f"  saved: {wav_path.name} ({duration:.1f}s)", flush=True)

            print("  transcribing...", flush=True)
            text = run_whisper(wav_path, args.lang, args.model, args.whisper_cli)
            if not text:
                print("  [empty transcript — whisper-cli error, see log]\n", flush=True)
                continue

            ipa = to_ipa(text, espeak_voice, args.espeak_ng, args.espeak_data)
            if ipa:
                payload = f"{text}\n[{ipa}]"
            else:
                payload = text
                logging.warning("empty IPA from espeak-ng for %r; falling back to text only", text)

            write_latest_transcript(payload, wav_path, args.lang)
            print(f"  text: {text}", flush=True)
            if ipa:
                print(f"  IPA : [{ipa}]", flush=True)
            else:
                print("  IPA : (espeak-ng error — see log)", flush=True)
            logging.info("transcribed %s: text=%r ipa=%r", wav_path.name, text, ipa)

            if args.no_auto_submit:
                print("  [auto-submit disabled; press Enter in Claude Code to inject]\n", flush=True)
            elif submit_to_claude_code(payload, args.window_title_re):
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
