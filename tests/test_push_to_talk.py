"""Unit tests for the pure logic in templates/scripts/push_to_talk.py.

Covers whisper JSON parsing. The language is now chosen by which hotkey is
held (no detection), so there is no resolution rule left to unit-test; the
push-to-talk dispatch is exercised manually. parse_whisper_json still has no
I/O or hardware dependencies, so it runs anywhere the voice-in Python deps
are importable.

Run:
    py -m pytest tests/test_push_to_talk.py
    # or, with no pytest installed:
    py tests/test_push_to_talk.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))

import push_to_talk as ptt  # noqa: E402


# --- parse_whisper_json ----------------------------------------------------

def test_parse_extracts_text_and_language():
    blob = (
        '{"result": {"language": "ru"}, '
        '"transcription": [{"text": " Привет"}, {"text": " мир"}]}'
    )
    text, lang = ptt.parse_whisper_json(blob)
    assert lang == "ru"
    assert text == "Привет мир"


def test_parse_handles_single_segment_english():
    blob = '{"result": {"language": "en"}, "transcription": [{"text": " Hello there."}]}'
    text, lang = ptt.parse_whisper_json(blob)
    assert lang == "en"
    assert text == "Hello there."


def test_parse_missing_result_returns_empty_lang():
    blob = '{"transcription": [{"text": "hi"}]}'
    text, lang = ptt.parse_whisper_json(blob)
    assert lang == ""
    assert text == "hi"


def test_parse_garbage_returns_empties():
    assert ptt.parse_whisper_json("not json") == ("", "")
    assert ptt.parse_whisper_json("") == ("", "")


def _run_all() -> int:
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
