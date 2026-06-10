"""Unit tests for the shared helpers in templates/scripts/cs_common.py.

Covers the project-config loader and the audio-device host-API ranking /
resolution that push_to_talk.py and speak_lang.py both rely on. Hardware-free:
a fake sounddevice is injected via cs_common.sd.

Run:
    py -m pytest tests/test_cs_common.py
    # or, with no pytest installed:
    py tests/test_cs_common.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))

import cs_common  # noqa: E402


# --- load_project_config ---------------------------------------------------

def test_load_project_config_missing_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert cs_common.load_project_config(Path(tmp) / "nope.json") == {}


def test_load_project_config_reads_dict():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claude_speech.json"
        path.write_text(json.dumps({"target_code": "nl", "common_code": "en"}), encoding="utf-8")
        cfg = cs_common.load_project_config(path)
        assert cfg["target_code"] == "nl"
        assert cfg["common_code"] == "en"


def test_load_project_config_bad_json_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claude_speech.json"
        path.write_text("{broken", encoding="utf-8")
        assert cs_common.load_project_config(path) == {}


def test_load_project_config_non_dict_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claude_speech.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert cs_common.load_project_config(path) == {}


# --- _host_api_rank --------------------------------------------------------

def test_host_api_rank_orders_modern_first():
    assert cs_common._host_api_rank("Windows WASAPI") < cs_common._host_api_rank("Windows DirectSound")
    assert cs_common._host_api_rank("Windows DirectSound") < cs_common._host_api_rank("MME")
    assert cs_common._host_api_rank("MME") < cs_common._host_api_rank("Windows WDM-KS")
    assert cs_common._host_api_rank("Windows WDM-KS") < cs_common._host_api_rank("ASIO")


def test_host_api_rank_ignores_spaces_and_hyphens():
    assert cs_common._host_api_rank("Windows WDM-KS") == cs_common._host_api_rank("wdmks")


# --- resolve_audio_device (host-API ranking, injected fake sounddevice) -----

class _FakeSd:
    def __init__(self, devices, hostapis):
        self._devices = devices
        self._hostapis = hostapis

    def query_devices(self, idx=None):
        return self._devices if idx is None else self._devices[idx]

    def query_hostapis(self, idx):
        return self._hostapis[idx]


def _usb_mic_table():
    base = {"max_input_channels": 1, "max_output_channels": 0}
    devices = [
        {**base, "name": "Microphone (USB PnP Audio Devic", "hostapi": 0},  # 0: MME
        {**base, "name": "Microphone (USB PnP Audio Device)", "hostapi": 1},  # 1: DirectSound
        {**base, "name": "Microphone (USB PnP Audio Device)", "hostapi": 2},  # 2: WASAPI
        {**base, "name": "Microphone (USB PnP Audio Device)", "hostapi": 3},  # 3: WDM-KS
    ]
    hostapis = [
        {"name": "MME"},
        {"name": "Windows DirectSound"},
        {"name": "Windows WASAPI"},
        {"name": "Windows WDM-KS"},
    ]
    return devices, hostapis


def _with_fake_sd(fake, fn):
    original = cs_common.sd
    cs_common.sd = fake
    try:
        return fn()
    finally:
        cs_common.sd = original


def test_resolve_audio_device_prefers_wasapi():
    devices, hostapis = _usb_mic_table()
    fake = _FakeSd(devices, hostapis)
    chosen = _with_fake_sd(fake, lambda: cs_common.resolve_audio_device("USB PnP", want_input=True))
    assert chosen == 2  # the WASAPI row


def test_resolve_audio_device_numeric_bypasses_ranking():
    devices, hostapis = _usb_mic_table()
    fake = _FakeSd(devices, hostapis)
    chosen = _with_fake_sd(fake, lambda: cs_common.resolve_audio_device("0", want_input=True))
    assert chosen == 0


def test_resolve_audio_device_empty_returns_none():
    assert cs_common.resolve_audio_device("", want_input=True) is None
    assert cs_common.resolve_audio_device(None, want_input=False) is None


def test_resolve_audio_device_no_match_raises():
    devices, hostapis = _usb_mic_table()
    fake = _FakeSd(devices, hostapis)

    def go():
        cs_common.resolve_audio_device("Nonexistent", want_input=True)

    try:
        _with_fake_sd(fake, go)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unmatched device name")


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
