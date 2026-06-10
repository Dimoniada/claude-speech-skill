"""Unit tests for install.py hook rendering.

Covers quote_interpreter_for_json (baking the real Python interpreter into the
settings.json hook commands instead of a bare `py`) and that the rendered
settings.json.tmpl is valid JSON whose hook commands invoke that interpreter.

Filesystem-free and hardware-free — safe to run anywhere.

Run:
    py -m pytest tests/test_install.py
    # or, with no pytest installed:
    py tests/test_install.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402


def test_quote_interpreter_round_trips_through_json():
    exe = r"C:\Program Files\Python312\python.exe"
    token = install.quote_interpreter_for_json(exe)
    # Embedded in a JSON string, it must parse back to the quoted, unescaped path.
    parsed = json.loads('{"command": "' + token + ' rest"}')
    assert parsed["command"] == r'"C:\Program Files\Python312\python.exe" rest'


def _render_settings(exe: str) -> dict:
    tpl = (install.TPL_DIR / "settings.json.tmpl").read_text(encoding="utf-8")
    mapping = {
        "VOICE": "nl-NL-FennaNeural",
        "LANG_CODE": "nl",
        "OUTPUT_DEVICE_ARG": "",
        "PY": install.quote_interpreter_for_json(exe),
    }
    rendered = install.render(tpl, mapping)
    return json.loads(rendered)  # also asserts the result is valid JSON


def test_rendered_hooks_use_real_interpreter_not_bare_py():
    exe = r"C:\Users\me\AppData\Local\Programs\Python\Python312\python.exe"
    data = _render_settings(exe)

    stop_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    inject_cmd = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

    quoted_exe = f'"{exe}"'
    for cmd, script in ((stop_cmd, "speak_lang.py"), (inject_cmd, "inject_transcript.py")):
        # The command starts by invoking the real interpreter, quoted.
        assert cmd.startswith(quoted_exe + " "), cmd
        # It is NOT a bare `py ...` launcher invocation any more.
        assert not cmd.startswith("py "), cmd
        # Still points at the right script via the portable project-dir var.
        assert script in cmd
        assert "$CLAUDE_PROJECT_DIR" in cmd


def test_rendered_settings_handles_spaces_in_interpreter_path():
    # A space in the path must stay inside the quotes (Program Files case).
    exe = r"C:\Program Files\Python312\python.exe"
    data = _render_settings(exe)
    stop_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert stop_cmd.startswith(f'"{exe}" ')


# --- claude_speech.json source-of-truth ------------------------------------

def test_build_config_records_codes_devices_and_hotkeys():
    config = install.build_config(
        target="Dutch", target_code="nl",
        common="English", common_code="en",
        voice="nl-NL-FennaNeural",
        input_device="USB PnP", output_device="OnePlus Bullets",
        target_hotkey="f9", common_hotkey="f10",
    )
    assert config["target_code"] == "nl"
    assert config["common_code"] == "en"
    assert config["voice"] == "nl-NL-FennaNeural"
    assert config["input_device"] == "USB PnP"
    assert config["output_device"] == "OnePlus Bullets"
    assert config["target_hotkey"] == "f9"
    assert config["common_hotkey"] == "f10"


def test_build_config_allows_null_devices():
    config = install.build_config(
        target="Dutch", target_code="nl",
        common="Russian", common_code="ru",
        voice="nl-NL-FennaNeural",
        input_device=None, output_device=None,
        target_hotkey="f9", common_hotkey="f10",
    )
    assert config["input_device"] is None
    assert config["output_device"] is None


def test_build_config_toolbar_defaults_enabled_claude_only():
    config = install.build_config(
        target="Dutch", target_code="nl", common="English", common_code="en",
        voice="nl-NL-FennaNeural", input_device=None, output_device=None,
        target_hotkey="f9", common_hotkey="f10",
    )
    assert config["selection_toolbar"] is True
    assert config["toolbar_window_re"] == install.DEFAULT_TOOLBAR_WINDOW_RE


def test_build_config_toolbar_disabled_and_everywhere():
    config = install.build_config(
        target="Dutch", target_code="nl", common="English", common_code="en",
        voice="nl-NL-FennaNeural", input_device=None, output_device=None,
        target_hotkey="f9", common_hotkey="f10",
        selection_toolbar=False, toolbar_window_re=None,
    )
    assert config["selection_toolbar"] is False
    assert config["toolbar_window_re"] is None


def test_write_config_round_trips_through_json():
    config = install.build_config(
        target="Dutch", target_code="nl",
        common="English", common_code="en",
        voice="nl-NL-FennaNeural",
        input_device="USB PnP", output_device=None,
        target_hotkey="f9", common_hotkey="f10",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".claude" / install.CONFIG_NAME
        install.write_config(path, config)
        assert json.loads(path.read_text(encoding="utf-8")) == config


def test_claude_md_marker_matches_the_config_codes():
    # The persona marker the daemon reads back must carry the SAME codes the
    # config records — that identity is what keeps persona and voice-in coupled.
    tpl = (install.TPL_DIR / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
    mapping = {
        "LANG_NAME": "Dutch", "LANG_CODE": "nl", "ISO": "nl-NL",
        "VOICE": "nl-NL-FennaNeural",
        "COMMON_NAME": "English", "COMMON_CODE": "en", "COMMON_ISO": "en-US",
    }
    rendered = install.render(tpl, mapping)
    m = re.search(r"<!--\s*claude-speech:\s*(.*?)\s*-->", rendered)
    assert m, "CLAUDE.md template is missing the claude-speech marker"
    fields = dict(tok.split("=", 1) for tok in m.group(1).split() if "=" in tok)
    config = install.build_config(
        target="Dutch", target_code="nl", common="English", common_code="en",
        voice="nl-NL-FennaNeural", input_device=None, output_device=None,
        target_hotkey="f9", common_hotkey="f10",
    )
    assert fields["target"] == config["target_code"]
    assert fields["common"] == config["common_code"]


if __name__ == "__main__":
    test_quote_interpreter_round_trips_through_json()
    test_rendered_hooks_use_real_interpreter_not_bare_py()
    test_rendered_settings_handles_spaces_in_interpreter_path()
    test_build_config_records_codes_devices_and_hotkeys()
    test_build_config_allows_null_devices()
    test_build_config_toolbar_defaults_enabled_claude_only()
    test_build_config_toolbar_disabled_and_everywhere()
    test_write_config_round_trips_through_json()
    test_claude_md_marker_matches_the_config_codes()
    print("ok")
