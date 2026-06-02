"""Unit tests for the pure logic in templates/scripts/push_to_talk.py.

Covers whisper JSON parsing and the audio-device host-API ranking that
disambiguates the same physical device exposed once per Windows host API
(MME, DirectSound, WASAPI, WDM-KS). The push-to-talk language dispatch is
chosen by which hotkey is held (no detection), so there is no resolution
rule left to unit-test there; it is exercised manually.

Run:
    py -m pytest tests/test_push_to_talk.py
    # or, with no pytest installed:
    py tests/test_push_to_talk.py
"""
from __future__ import annotations

import socket
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


# --- _host_api_rank --------------------------------------------------------

def test_host_api_rank_orders_modern_first():
    # WASAPI is the modern Windows audio endpoint and the only one that works
    # reliably for Bluetooth output; WDM-KS is exclusive-mode and the most
    # likely to refuse to open. Anything unknown lands at the bottom.
    assert ptt._host_api_rank("Windows WASAPI") < ptt._host_api_rank("Windows DirectSound")
    assert ptt._host_api_rank("Windows DirectSound") < ptt._host_api_rank("MME")
    assert ptt._host_api_rank("MME") < ptt._host_api_rank("Windows WDM-KS")
    assert ptt._host_api_rank("Windows WDM-KS") < ptt._host_api_rank("ASIO")


def test_host_api_rank_ignores_spaces_and_hyphens():
    # PortAudio reports "Windows WDM-KS" with a hyphen, but the rank table
    # uses the bare key "wdmks" — both forms must compare equal.
    assert ptt._host_api_rank("Windows WDM-KS") == ptt._host_api_rank("wdmks")
    assert ptt._host_api_rank("WindowsWASAPI") == ptt._host_api_rank("Windows WASAPI")


# --- resolve_audio_device host-API ranking ---------------------------------

class _FakeSd:
    """Drop-in for sounddevice that returns canned device + hostapi tables.

    The real sounddevice import touches PortAudio and probes hardware on
    import; we swap it in via ``ptt.sd = _FakeSd(...)`` for the duration of
    a single test so the ranking logic can be exercised without a sound card.
    """

    def __init__(self, devices, hostapis):
        self._devices = devices
        self._hostapis = hostapis

    def query_devices(self, idx=None):
        return self._devices if idx is None else self._devices[idx]

    def query_hostapis(self, idx):
        return self._hostapis[idx]


def _usb_mic_quadruple():
    """The bug scenario: one USB mic exposed once per host API.

    Indices and host APIs mirror what PortAudio prints on a real Windows box:
    MME is the lowest index (and its name is truncated to 31 chars), WASAPI
    sits higher up at index 9. The old "lowest index wins" rule picked MME
    and silently dropped capture; the fix must pick WASAPI (index 9).
    """
    base = {"max_input_channels": 1, "max_output_channels": 0}
    devices = [
        # 0..0: filler so MME isn't at index 0 (matches real layout)
        {"name": "Sound Mapper", "max_input_channels": 2, "max_output_channels": 0, "hostapi": 0},
        # 1: MME — name truncated to 31 chars by PortAudio
        {**base, "name": "Microphone (USB PnP Audio Devic", "hostapi": 0},
        # 2..4: filler outputs so "USB PnP" only matches the four mic rows
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 1},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 2},
        # 5: DirectSound
        {**base, "name": "Microphone (USB PnP Audio Device)", "hostapi": 1},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 1},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 2},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 2},
        # 9: WASAPI — the one we want
        {**base, "name": "Microphone (USB PnP Audio Device)", "hostapi": 2},
        # 10..16: filler
        *[{"name": f"slot {i}", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 3}
          for i in range(10, 17)],
        # 17: WDM-KS
        {**base, "name": "Microphone (USB PnP Audio Device)", "hostapi": 3},
    ]
    hostapis = [
        {"name": "MME"},
        {"name": "Windows DirectSound"},
        {"name": "Windows WASAPI"},
        {"name": "Windows WDM-KS"},
    ]
    return devices, hostapis


def _with_fake_sd(fake, fn):
    """Run ``fn`` with ``ptt.sd`` swapped for ``fake`` and restored after.

    Plain try/finally instead of pytest's monkeypatch so the file still runs
    under the bare ``py tests/test_push_to_talk.py`` path used by _run_all.
    """
    original = ptt.sd
    ptt.sd = fake
    try:
        return fn()
    finally:
        ptt.sd = original


def test_resolve_audio_device_prefers_wasapi_over_lower_index_mme():
    # This is the regression: "USB PnP" matches indices 1 (MME), 5 (DS),
    # 9 (WASAPI), 17 (WDM-KS). Old behaviour returned 1 and BT mic capture
    # silently failed. Correct behaviour returns 9.
    devices, hostapis = _usb_mic_quadruple()
    fake = _FakeSd(devices, hostapis)
    chosen = _with_fake_sd(fake, lambda: ptt.resolve_audio_device("USB PnP", want_input=True))
    assert chosen == 9, f"expected WASAPI mic at 9, got {chosen}"


def test_resolve_audio_device_ranking_works_for_output_too():
    # Same fake table, but query as output. The "Speakers" name matches
    # outputs at 2 (MME), 3+6 (DS), 4+7+8 (WASAPI). WASAPI wins; among the
    # three WASAPI matches the lowest index (4) breaks the tie.
    devices, hostapis = _usb_mic_quadruple()
    fake = _FakeSd(devices, hostapis)
    chosen = _with_fake_sd(fake, lambda: ptt.resolve_audio_device("Speakers", want_input=False))
    assert chosen == 4, f"expected WASAPI speaker at 4, got {chosen}"


def test_resolve_audio_device_numeric_spec_bypasses_ranking():
    # A user who pins by index is asking for that exact endpoint — even if it
    # is the MME one we'd otherwise demote. Ranking must NOT override them.
    devices, hostapis = _usb_mic_quadruple()
    fake = _FakeSd(devices, hostapis)
    chosen = _with_fake_sd(fake, lambda: ptt.resolve_audio_device("1", want_input=True))
    assert chosen == 1


# --- resolve_hotkey --------------------------------------------------------

def test_resolve_hotkey_function_key():
    # The F9/F10 defaults and any fN remap must map to the pynput Key constant.
    assert ptt.resolve_hotkey("f9") == ptt.keyboard.Key.f9
    assert ptt.resolve_hotkey("f10") == ptt.keyboard.Key.f10


def test_resolve_hotkey_named_key():
    assert ptt.resolve_hotkey("space") == ptt.keyboard.Key.space


def test_resolve_hotkey_is_case_insensitive():
    # The setup interview may hand us "F7"; resolve_hotkey lowercases first.
    assert ptt.resolve_hotkey("F7") == ptt.keyboard.Key.f7


def test_resolve_hotkey_single_char():
    # A letter remap becomes a KeyCode carrying that character.
    key = ptt.resolve_hotkey("a")
    assert isinstance(key, ptt.keyboard.KeyCode)
    assert key.char == "a"


def test_resolve_hotkey_unknown_raises():
    # An unrecognised multi-char name is rejected, so a bad remap fails loudly
    # at launch rather than silently listening for a key that never fires.
    try:
        ptt.resolve_hotkey("notakey")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown hotkey")


# --- find_free_port --------------------------------------------------------

def test_find_free_port_returns_preferred_when_free():
    # Grab an OS-assigned port, release it, then confirm find_free_port hands
    # that same (now-free) port straight back without moving on.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert ptt.find_free_port("127.0.0.1", free) == free


def test_find_free_port_skips_occupied():
    # Hold a port open, then ask find_free_port to start there — it must skip
    # the busy one and return a higher, free port instead of failing.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        taken = occupied.getsockname()[1]
        chosen = ptt.find_free_port("127.0.0.1", taken, attempts=10)
        assert chosen is not None
        assert chosen != taken
        assert chosen > taken


def test_find_free_port_none_when_range_exhausted():
    # attempts=1 with the only candidate occupied -> None, so the caller can
    # fall back to the requested port and fail loudly rather than hang.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        taken = occupied.getsockname()[1]
        assert ptt.find_free_port("127.0.0.1", taken, attempts=1) is None


# --- find_chat_input cold-start retry --------------------------------------

class _FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeElementInfo:
    def __init__(self, focusable):
        # Mirror pywinauto's g.element_info.element.CurrentIsKeyboardFocusable
        self.element = type("E", (), {"CurrentIsKeyboardFocusable": focusable})()


class _FakeGroup:
    def __init__(self, rect, focusable):
        self._rect = rect
        self.element_info = _FakeElementInfo(focusable)

    def rectangle(self):
        return self._rect


class _FakeWin:
    """A Claude-app window whose UIA tree only yields the input Group after a
    few walks — modelling Chromium's accessibility tree not being realized on
    the first descendants() walk right after the window is foregrounded.
    """

    def __init__(self, groups_per_call):
        # groups_per_call: list of group-lists, one per descendants() call;
        # the last entry is reused for any calls beyond its length.
        self._groups_per_call = groups_per_call
        self.calls = 0

    def rectangle(self):
        return _FakeRect(0, 0, 1000, 1000)  # 1000x1000 window at origin

    def descendants(self, control_type=None):  # noqa: ANN001
        idx = min(self.calls, len(self._groups_per_call) - 1)
        self.calls += 1
        return self._groups_per_call[idx]


def _valid_input_group():
    # Wide (>=400px), low (top >= 720), focusable -> matches the input filter.
    return _FakeGroup(_FakeRect(50, 800, 950, 900), focusable=True)


def _narrow_button_group():
    # A toolbar button: focusable and low, but too narrow to match.
    return _FakeGroup(_FakeRect(50, 800, 120, 900), focusable=True)


def test_find_chat_input_retries_until_tree_warms_up():
    # Empty on the first two walks (cold Chromium tree), input appears on the
    # third -- find_chat_input must keep re-walking and return it instead of
    # giving up after the first cold walk (the "only works the 2nd time" bug).
    win = _FakeWin([[], [], [_valid_input_group()]])
    found = ptt.find_chat_input(win, retries=4, retry_delay=0)
    assert found is not None
    assert win.calls == 3


def test_find_chat_input_returns_none_when_never_appears():
    # If the input never shows up within `retries`, return None so the caller
    # falls back to window-level focus rather than looping forever.
    win = _FakeWin([[]])
    assert ptt.find_chat_input(win, retries=3, retry_delay=0) is None
    assert win.calls == 3


def test_find_chat_input_does_not_retry_when_ambiguous():
    # Two wide focusable Groups is a different problem (ambiguous), not a cold
    # tree -- retrying can't help, so bail on the first walk.
    win = _FakeWin([[_valid_input_group(), _valid_input_group()]])
    assert ptt.find_chat_input(win, retries=4, retry_delay=0) is None
    assert win.calls == 1


def test_find_chat_input_first_call_hit_skips_retry():
    # When the input is present on the very first walk (warm app), return it
    # immediately without spending any retries.
    win = _FakeWin([[_narrow_button_group(), _valid_input_group()]])
    found = ptt.find_chat_input(win, retries=4, retry_delay=0)
    assert found is not None
    assert win.calls == 1


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
