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
import sys
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


if __name__ == "__main__":
    test_quote_interpreter_round_trips_through_json()
    test_rendered_hooks_use_real_interpreter_not_bare_py()
    test_rendered_settings_handles_spaces_in_interpreter_path()
    print("ok")
