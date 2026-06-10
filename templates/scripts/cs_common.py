"""Shared helpers for the claude-speech project scripts.

Single home for code that push_to_talk.py, speak_lang.py and selection_toolbar.py
all need, so it isn't copy-pasted three ways:
  - load_project_config: read the project's .claude/claude_speech.json
  - audio-device helpers: host-API ranking, device resolution, device listing

This file lives in scripts/ next to those scripts; each imports it by name (the
script's own directory is on sys.path when it runs, or it inserts it explicitly).

sounddevice is imported lazily on first audio call (cached in the module global
`sd`), so importing this module stays cheap for callers that never touch audio —
notably the TTS Stop hook's default MCI path, which must not pull in PortAudio.
Tests inject a fake by assigning `cs_common.sd` before calling.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / ".claude" / "claude_speech.json"

# Lazily populated handle to the sounddevice module (see _ensure_sd). Tests set
# this to a fake before exercising the audio helpers.
sd = None


def load_project_config(path: Path = CONFIG_PATH) -> dict:
    """Read .claude/claude_speech.json — the single source of truth install.py
    writes next to CLAUDE.md. A missing or unreadable file yields {}, so callers
    fall back to CLI args / built-in defaults instead of failing."""
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("could not read project config %s: %s", path, exc)
        return {}


def _ensure_sd():
    """Lazy-import sounddevice into the module global `sd` (cached). Keeps import
    of this module cheap; lets tests inject a fake by pre-setting cs_common.sd."""
    global sd
    if sd is None:
        import sounddevice
        sd = sounddevice
    return sd


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


def format_device_list(include_inputs: bool = True, include_outputs: bool = True) -> str:
    """Human-readable listing of input and/or output audio devices."""
    sd = _ensure_sd()
    devices = sd.query_devices()
    lines: list[str] = []
    if include_inputs:
        lines.append("Input devices (microphones — for push-to-talk):")
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                host = sd.query_hostapis(d["hostapi"])["name"]
                lines.append(f"  [{i}] {d['name']}  (in={d['max_input_channels']}, {host})")
    if include_outputs:
        lines.append("Output devices (speakers/headphones — for TTS playback):")
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                host = sd.query_hostapis(d["hostapi"])["name"]
                lines.append(f"  [{i}] {d['name']}  (out={d['max_output_channels']}, {host})")
    return "\n".join(lines)


def resolve_audio_device(spec, want_input: bool):
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
    sd = _ensure_sd()
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
