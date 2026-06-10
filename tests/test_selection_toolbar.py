"""Unit tests for the pure logic in templates/scripts/selection_toolbar.py.

Covers selection/drag detection, the window scope gate, on-screen clamping,
clipboard capture (with the user's clipboard restored), the project-config
reader, and the speak_text wrapper (which reuses speak_lang). The tkinter UI
itself is exercised manually — only the injectable, hardware-free helpers are
unit-tested here.

Run:
    py -m pytest tests/test_selection_toolbar.py
    # or, with no pytest installed:
    py tests/test_selection_toolbar.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))

import selection_toolbar as st  # noqa: E402


# --- is_drag ---------------------------------------------------------------

def test_is_drag_true_past_threshold():
    assert st.is_drag((0, 0), (10, 0), threshold=6) is True
    assert st.is_drag((0, 0), (0, 7), threshold=6) is True


def test_is_drag_false_below_threshold():
    # A near-stationary click (a couple px of jitter) is not a selection.
    assert st.is_drag((100, 100), (102, 101), threshold=6) is False


def test_is_drag_true_exactly_at_threshold():
    # Exactly `threshold` px of travel counts (>=).
    assert st.is_drag((0, 0), (6, 0), threshold=6) is True


# --- selection_anchor ------------------------------------------------------

def test_selection_anchor_left_to_right_drag():
    # Normal drag (start top-left, end bottom-right) -> the release point.
    assert st.selection_anchor((100, 200), (300, 240)) == (300, 240)


def test_selection_anchor_right_to_left_drag():
    # Dragging backwards must still anchor at the bottom-right of the box.
    assert st.selection_anchor((300, 240), (100, 200)) == (300, 240)


def test_selection_anchor_mixed_directions():
    assert st.selection_anchor((100, 240), (300, 200)) == (300, 240)
    assert st.selection_anchor((300, 200), (100, 240)) == (300, 240)


# --- window_allowed --------------------------------------------------------

def test_window_allowed_any_app_when_no_regex():
    assert st.window_allowed("Anything at all", None) is True
    assert st.window_allowed("", None) is True


def test_window_allowed_matches_regex():
    assert st.window_allowed("Claude", r".*Claude.*") is True
    assert st.window_allowed("Notepad", r".*Claude.*") is False


def test_window_allowed_handles_empty_title_with_regex():
    assert st.window_allowed("", r".*Claude.*") is False


def test_default_scope_requires_title_to_start_with_claude():
    # Regression: the Claude-only default must NOT fire in a browser whose title
    # merely *contains* "Claude" mid-string (the old `.*Claude.*` did). Only the
    # app, whose window title starts with "Claude", should match.
    re_ = st.DEFAULT_TOOLBAR_WINDOW_RE
    assert st.window_allowed("Claude", re_) is True
    assert st.window_allowed("Claude — Anthropic", re_) is True
    assert st.window_allowed("Anthropic's Claude - Google Chrome", re_) is False
    assert st.window_allowed("Reddit - r/Claude — Firefox", re_) is False


# --- resolve_window_re -----------------------------------------------------

def test_resolve_window_re_cli_value_wins():
    assert st.resolve_window_re(".*Foo.*", {"toolbar_window_re": None}) == ".*Foo.*"


def test_resolve_window_re_cli_empty_means_any_app():
    assert st.resolve_window_re("", {}) is None


def test_resolve_window_re_from_config_null_is_any_app():
    assert st.resolve_window_re(None, {"toolbar_window_re": None}) is None


def test_resolve_window_re_from_config_value():
    assert st.resolve_window_re(None, {"toolbar_window_re": ".*Claude.*"}) == ".*Claude.*"


def test_resolve_window_re_defaults_to_claude_when_absent():
    assert st.resolve_window_re(None, {}) == st.DEFAULT_TOOLBAR_WINDOW_RE


# --- clamp_to_screen -------------------------------------------------------

def test_clamp_to_screen_within_bounds_unchanged():
    assert st.clamp_to_screen(100, 100, 80, 30, 1920, 1080) == (100, 100)


def test_clamp_to_screen_pulls_overflow_back():
    # A popup near the bottom-right edge is nudged fully on-screen.
    x, y = st.clamp_to_screen(1900, 1070, 80, 30, 1920, 1080, margin=4)
    assert x == 1920 - 80 - 4
    assert y == 1080 - 30 - 4


def test_clamp_to_screen_respects_left_top_margin():
    assert st.clamp_to_screen(-50, -50, 80, 30, 1920, 1080, margin=4) == (4, 4)


# --- load_project_config ---------------------------------------------------

def test_load_project_config_missing_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert st.load_project_config(Path(tmp) / "nope.json") == {}


def test_load_project_config_reads_dict():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claude_speech.json"
        path.write_text(json.dumps({"voice": "nl-NL-FennaNeural", "output_device": "OnePlus"}), encoding="utf-8")
        cfg = st.load_project_config(path)
        assert cfg["voice"] == "nl-NL-FennaNeural"
        assert cfg["output_device"] == "OnePlus"


def test_load_project_config_bad_json_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claude_speech.json"
        path.write_text("{broken", encoding="utf-8")
        assert st.load_project_config(path) == {}


# --- capture_selection -----------------------------------------------------

def test_capture_selection_returns_text_and_restores_clipboard():
    clip = {"value": "user's earlier clipboard"}

    def copy_fn():
        # Simulate Ctrl+C landing the on-screen selection on the clipboard.
        clip["value"] = "selected Dutch text"

    def get_clip():
        return clip["value"]

    def set_clip(v):
        clip["value"] = v

    prev = get_clip()
    captured = st.capture_selection(copy_fn, get_clip, set_clip, prev)
    assert captured == "selected Dutch text"
    # The user's original clipboard must be put back.
    assert clip["value"] == "user's earlier clipboard"


def test_capture_selection_restores_empty_when_prev_none():
    clip = {"value": "x"}
    st.capture_selection(lambda: clip.__setitem__("value", "sel"),
                         lambda: clip["value"], lambda v: clip.__setitem__("value", v),
                         None)
    assert clip["value"] == ""


# --- speak_text (reuses speak_lang) ----------------------------------------

class _FakeSpeakLang:
    def __init__(self, device=7, resolve_raises=False):
        self._device = device
        self._resolve_raises = resolve_raises
        self.calls: dict = {}

    def resolve_output_device(self, spec):
        self.calls["resolve"] = spec
        if self._resolve_raises:
            raise ValueError("bad device")
        return self._device

    async def synthesize(self, text, voice, rate, out):
        self.calls["synthesize"] = (text, voice, rate)

    def play_mp3(self, path, device):
        self.calls["play_device"] = device


def test_speak_text_invokes_speak_lang_with_config():
    fake = _FakeSpeakLang(device=7)
    ok = st.speak_text("Hallo", "nl-NL-FennaNeural", "OnePlus", "-10%", _speak_lang=fake)
    assert ok is True
    assert fake.calls["synthesize"] == ("Hallo", "nl-NL-FennaNeural", "-10%")
    assert fake.calls["resolve"] == "OnePlus"
    assert fake.calls["play_device"] == 7


def test_speak_text_falls_back_to_default_device_on_resolve_error():
    fake = _FakeSpeakLang(resolve_raises=True)
    ok = st.speak_text("Hallo", "nl-NL-FennaNeural", "bogus", "-10%", _speak_lang=fake)
    assert ok is True
    assert fake.calls["play_device"] is None  # None -> speak_lang's system-default path


def test_speak_text_noop_without_text_or_voice():
    fake = _FakeSpeakLang()
    assert st.speak_text("", "nl-NL-FennaNeural", None, "-10%", _speak_lang=fake) is False
    assert st.speak_text("Hallo", "", None, "-10%", _speak_lang=fake) is False
    assert "synthesize" not in fake.calls


# --- translate_text --------------------------------------------------------

def test_translate_text_uses_injected_translator():
    out = st.translate_text("Hallo", "nl", "en", translate_fn=lambda t, f, to: f"[{f}->{to}] {t}")
    assert out == "[nl->en] Hallo"


def test_translate_text_empty_or_missing_codes_returns_empty():
    assert st.translate_text("", "nl", "en", translate_fn=lambda *a: "x") == ""
    assert st.translate_text("Hallo", "", "en", translate_fn=lambda *a: "x") == ""
    assert st.translate_text("Hallo", "nl", "", translate_fn=lambda *a: "x") == ""


def test_translate_text_swallows_translator_error():
    def boom(*_a):
        raise RuntimeError("offline model missing")
    assert st.translate_text("Hallo", "nl", "en", translate_fn=boom) == ""


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
