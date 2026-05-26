"""UserPromptSubmit hook: inject the latest voice transcript into Claude's context.

Reads recordings/latest_transcript.txt (written by push_to_talk.py) and
echoes its content to stdout, wrapped in a <voice-input> block so the
assistant can clearly distinguish spoken input from typed input.

Anything printed to stdout from a UserPromptSubmit hook is prepended to
the user's prompt as additional context for the assistant.

No freshness or already-seen gate — every Enter re-injects whatever is
currently in latest_transcript.txt. Same transcript twice is acceptable;
the user controls the loop.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Transcripts may contain IPA (non-ASCII). Force UTF-8 on stdout so the
# hook output Claude Code consumes doesn't get mangled by codepage.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
LATEST_TRANSCRIPT = PROJECT_ROOT / "recordings" / "latest_transcript.txt"
LOG_PATH = PROJECT_ROOT / "logs" / "inject_transcript.log"


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> int:
    setup_logging()

    # Drain stdin so Claude Code doesn't choke on a blocked pipe; we don't
    # actually need anything from the payload for this hook.
    try:
        _ = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pass

    if not LATEST_TRANSCRIPT.is_file():
        logging.info("no transcript file at %s; skipping", LATEST_TRANSCRIPT)
        return 0

    try:
        content = LATEST_TRANSCRIPT.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logging.error("could not read transcript: %s", exc)
        return 0

    if not content:
        logging.info("transcript file is empty; skipping")
        return 0

    # Anything printed here is prepended to the user's prompt as context.
    sys.stdout.write("<voice-input>\n")
    sys.stdout.write(content)
    sys.stdout.write("\n</voice-input>\n")
    logging.info("injected %d chars", len(content))
    return 0


if __name__ == "__main__":
    sys.exit(main())
