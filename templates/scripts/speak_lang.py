"""Stop hook: extract tagged target-language text from Claude's last reply and speak it.

Reads Claude Code Stop-hook JSON from stdin, finds the most recent assistant
message in the transcript, pulls all text inside <{tag}>...</{tag}> markers,
synthesizes it with edge-tts, and plays the result via the Windows MCI interface.

Exits silently if the transcript has no language markers, so sessions that
don't use the configured tag stay quiet.

Usage (wired into .claude/settings.json):
    py speak_lang.py --voice nl-NL-FennaNeural --tag nl [--rate -10%]
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


def extract_tagged(text: str, pattern: re.Pattern[str]) -> str:
    matches = pattern.findall(text)
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


def play_mp3(path: Path) -> None:
    """Synchronous MP3 playback via Windows MCI (no extra deps)."""
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="claude-speech Stop-hook TTS")
    parser.add_argument("--voice", required=True, help="edge-tts voice id (e.g. nl-NL-FennaNeural)")
    parser.add_argument("--tag", required=True, help="lowercase tag (e.g. nl, de, es) — extracts <tag>...</tag>")
    parser.add_argument("--rate", default=DEFAULT_RATE, help="edge-tts rate string, e.g. -10%% or +5%%")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging()
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
        play_mp3(out)
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
