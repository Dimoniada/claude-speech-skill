"""Stop hook: extract tagged target-language text from Claude's last reply and speak it.

Reads Claude Code Stop-hook JSON from stdin, finds the most recent assistant
message in the transcript, pulls all text inside <{tag}>...</{tag}> markers,
synthesizes it with edge-tts, and plays the result via the Windows MCI interface.

Exits silently if the transcript has no language markers, so sessions that
don't use the configured tag stay quiet.

Usage (wired into .claude/settings.json):
    py speak_lang.py --voice nl-NL-FennaNeural --tag nl [--rate -10%]
    py speak_lang.py --voice nl-NL-FennaNeural --tag nl --output-device "Headphones"
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import re
import sys
import tempfile
import time
from ctypes import c_buffer
from pathlib import Path

DEFAULT_RATE = "-10%"
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR.parent / "logs" / "speak_lang.log"

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


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_tag_regex(tag: str) -> re.Pattern[str]:
    safe = re.escape(tag)
    return re.compile(rf"<{safe}>(.*?)</{safe}>", re.DOTALL | re.IGNORECASE)


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def strip_markdown_code(text: str) -> str:
    """Remove Markdown code fences and inline-code spans from text.

    The assistant often mentions the language tag literally inside backticks
    when explaining how the skill works (e.g. `<en>` blocks). Without this
    strip, a bare `<en>` in prose would pair with the next `</en>` and
    cause the regex to swallow all intervening text into the spoken output.
    """
    text = _CODE_FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def extract_tagged(text: str, pattern: re.Pattern[str]) -> str:
    cleaned = strip_markdown_code(text)
    matches = pattern.findall(cleaned)
    return " ".join(m.strip() for m in matches if m.strip())


def last_assistant_text(transcript_path: Path) -> str:
    last = ""
    with transcript_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            chunks = []
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
            if chunks:
                last = "\n".join(chunks)
    return last


async def synthesize(text: str, voice: str, rate: str, out_path: Path) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(out_path))


def play_mp3_mci(path: Path) -> None:
    """Synchronous MP3 playback via Windows MCI (no extra deps).

    Always uses the system default output device — MCI has no clean way to
    route to a specific endpoint. Used when no --output-device is requested.
    """
    alias = f"snd_{int(time.time() * 1000)}"
    buf = c_buffer(255)
    mci = ctypes.windll.winmm.mciSendStringW
    open_cmd = f'open "{path}" type mpegvideo alias {alias}'
    if mci(open_cmd, buf, 254, 0) != 0:
        raise RuntimeError(f"MCI open failed for {path}")
    try:
        mci(f"play {alias} wait", buf, 254, 0)
    finally:
        mci(f"close {alias}", buf, 254, 0)


def resolve_output_device(spec: str | None) -> int | None:
    """Resolve an output-device spec (index or name substring) to an index.

    Returns None for an empty spec (caller uses MCI / system default). Device
    indices are not stable across reboots, so a name substring is preferred.
    Raises ValueError on an invalid index or a name that matches nothing.
    Logs and picks the lowest index when a name matches several endpoints
    (the same hardware is usually exposed once per host API).
    """
    if not spec:
        return None
    import sounddevice as sd
    spec = str(spec).strip()
    devices = sd.query_devices()
    if spec.isdigit():
        idx = int(spec)
        if idx < 0 or idx >= len(devices):
            raise ValueError(f"output device index {idx} out of range (0..{len(devices) - 1})")
        if devices[idx]["max_output_channels"] <= 0:
            raise ValueError(f"device [{idx}] {devices[idx]['name']!r} has no output channels")
        return idx
    needle = spec.lower()
    matches = [
        i for i, d in enumerate(devices)
        if d["max_output_channels"] > 0 and needle in d["name"].lower()
    ]
    if not matches:
        raise ValueError(f"no output device name contains {spec!r}")
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
            "output device %r matched several; picked [%d] %s (%s). Candidates: %s",
            spec, chosen, devices[chosen]["name"],
            sd.query_hostapis(devices[chosen]["hostapi"])["name"], alts,
        )
    return matches[0]


def play_mp3_sounddevice(path: Path, device: int) -> None:
    """Decode the MP3 to PCM (miniaudio) and play it on a specific output
    device via sounddevice. Blocks until playback finishes.

    WASAPI shared mode requires audio at the endpoint's configured rate (often
    48 kHz for Bluetooth/USB) and rejects edge-tts MP3s (24 kHz) with
    "Invalid sample rate". Pass WasapiSettings(auto_convert=True) so Windows
    resamples for us. Other host APIs (DirectSound, MME) resample at the
    system mixer and accept any rate.
    """
    import miniaudio
    import numpy as np
    import sounddevice as sd

    decoded = miniaudio.decode_file(str(path))  # int16 PCM at the file's native rate
    samples = np.frombuffer(bytes(decoded.samples), dtype=np.int16)
    if decoded.nchannels > 1:
        samples = samples.reshape(-1, decoded.nchannels)

    extra_settings = None
    host_api_name = sd.query_hostapis(sd.query_devices(device)["hostapi"])["name"].lower()
    if "wasapi" in host_api_name:
        extra_settings = sd.WasapiSettings(auto_convert=True)

    sd.play(samples, decoded.sample_rate, device=device, extra_settings=extra_settings)
    sd.wait()


def play_mp3(path: Path, output_device: int | None) -> None:
    """Play an MP3 file. Uses sounddevice (specific endpoint) when an output
    device is given, otherwise the dependency-free Windows MCI default path."""
    if output_device is None:
        play_mp3_mci(path)
    else:
        play_mp3_sounddevice(path, output_device)


def format_device_list() -> str:
    """Human-readable listing of output audio devices (for --list-devices)."""
    import sounddevice as sd
    lines = ["Output devices (speakers/headphones — for TTS playback):"]
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            host = sd.query_hostapis(d["hostapi"])["name"]
            lines.append(f"  [{i}] {d['name']}  (out={d['max_output_channels']}, {host})")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="claude-speech Stop-hook TTS")
    # --voice/--tag are required for playback but not for --list-devices, so
    # they are validated in main() rather than marked required here.
    parser.add_argument("--voice", help="edge-tts voice id (e.g. nl-NL-FennaNeural)")
    parser.add_argument("--tag", help="lowercase tag (e.g. nl, de, es) — extracts <tag>...</tag>")
    parser.add_argument("--rate", default=DEFAULT_RATE, help="edge-tts rate string, e.g. -10%% or +5%%")
    parser.add_argument(
        "--output-device",
        default=None,
        help="speaker/headphone to play TTS on: device index or a substring of its name (default: system default via MCI). See --list-devices.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="print available audio output devices and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.list_devices:
        print(format_device_list())
        return 0

    setup_logging()

    if not args.voice or not args.tag:
        logging.error("--voice and --tag are required for playback")
        return 0

    try:
        output_device = resolve_output_device(args.output_device)
    except ValueError as exc:
        logging.error("output device: %s", exc)
        return 0

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        logging.error("bad hook payload: %s", exc)
        return 0

    transcript_path = Path(payload.get("transcript_path", ""))
    if not transcript_path.is_file():
        logging.info("no transcript at %s", transcript_path)
        return 0

    raw = last_assistant_text(transcript_path)
    pattern = build_tag_regex(args.tag)
    text = extract_tagged(raw, pattern)
    if not text:
        logging.info("no <%s> tags in last assistant message; skipping", args.tag)
        return 0

    logging.info("speaking %d chars via %s", len(text), args.voice)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        asyncio.run(synthesize(text, args.voice, args.rate, out))
        play_mp3(out, output_device)
    except Exception as exc:
        logging.exception("TTS failed: %s", exc)
        return 0
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
