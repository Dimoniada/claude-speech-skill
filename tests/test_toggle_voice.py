"""Unit tests for the voice-output on/off toggle.

Covers toggle_voice.py (the `/claude-speech off` + re-enable mechanism) and
install.ensure_stop_hook (re-adding the Stop hook to an existing settings.json
when the installer is re-run with language args after a prior `off`).

These touch the filesystem but only inside a throwaway temp dir, so they run
anywhere — no audio hardware, no whisper, no network.

Run:
    py -m pytest tests/test_toggle_voice.py
    # or, with no pytest installed:
    py tests/test_toggle_voice.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import toggle_voice  # noqa: E402
import install  # noqa: E402

SPEAK_CMD = r'py "$CLAUDE_PROJECT_DIR\scripts\speak_lang.py" --voice en-US-JennyNeural --tag en'
INJECT_CMD = r'py "$CLAUDE_PROJECT_DIR\scripts\inject_transcript.py"'


def _settings_with_voice() -> dict:
    return {
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": SPEAK_CMD}]}],
            "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": INJECT_CMD}]}],
        },
        "model": "claude-opus-4-8",
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _speak_hooks(settings: dict) -> list[dict]:
    stop = (settings.get("hooks") or {}).get("Stop") or []
    return [h for g in stop for h in (g.get("hooks") or []) if "speak_lang.py" in (h.get("command") or "")]


# --- toggle_voice --off ----------------------------------------------------

def test_off_removes_speak_hook_and_stashes():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        _write(settings, _settings_with_voice())

        toggle_voice.main(["--project-dir", str(proj), "--off"])

        after = _read(settings)
        assert _speak_hooks(after) == [], "speak hook should be removed"
        assert "UserPromptSubmit" in after["hooks"], "other hooks must be preserved"
        assert after["model"] == "claude-opus-4-8", "unrelated keys must be preserved"

        stash = proj / ".claude" / "speak_lang.hook.json"
        assert stash.exists(), "removed hook must be stashed"
        stashed = json.loads(stash.read_text(encoding="utf-8"))
        assert stashed[0]["hooks"][0]["command"] == SPEAK_CMD


def test_off_drops_empty_stop_key():
    # When the only Stop hook was the speak hook, the Stop key should not be
    # left as an empty list dangling in the file.
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        data = _settings_with_voice()
        _write(settings, data)
        toggle_voice.main(["--project-dir", str(proj), "--off"])
        assert "Stop" not in _read(settings)["hooks"]


def test_off_idempotent_when_already_off():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        _write(settings, _settings_with_voice())
        toggle_voice.main(["--project-dir", str(proj), "--off"])
        # Second off must not raise and must leave the stash in place.
        toggle_voice.main(["--project-dir", str(proj), "--off"])
        assert (proj / ".claude" / "speak_lang.hook.json").exists()


def test_off_no_settings_file_is_noop():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        rc = toggle_voice.main(["--project-dir", str(proj), "--off"])
        assert rc == 0
        assert not (proj / ".claude" / "speak_lang.hook.json").exists()


# --- toggle_voice --on -----------------------------------------------------

def test_on_restores_and_deletes_stash():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        _write(settings, _settings_with_voice())
        toggle_voice.main(["--project-dir", str(proj), "--off"])
        toggle_voice.main(["--project-dir", str(proj), "--on"])

        after = _read(settings)
        hooks = _speak_hooks(after)
        assert len(hooks) == 1 and hooks[0]["command"] == SPEAK_CMD
        assert "UserPromptSubmit" in after["hooks"]
        assert not (proj / ".claude" / "speak_lang.hook.json").exists(), "stash consumed on restore"


def test_on_is_deduped():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        _write(settings, _settings_with_voice())
        toggle_voice.main(["--project-dir", str(proj), "--off"])
        toggle_voice.main(["--project-dir", str(proj), "--on"])
        # A second --on has nothing stashed; must not add a duplicate.
        toggle_voice.main(["--project-dir", str(proj), "--on"])
        assert len(_speak_hooks(_read(settings))) == 1


def test_on_without_stash_is_noop():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        rc = toggle_voice.main(["--project-dir", str(proj), "--on"])
        assert rc == 0


# --- install.ensure_stop_hook ---------------------------------------------

def test_ensure_stop_hook_merges_when_missing():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        # Simulate state after `off`: speak hook gone, others remain.
        _write(settings, {
            "hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": INJECT_CMD}]}]},
            "model": "claude-opus-4-8",
        })
        rendered = json.dumps(_settings_with_voice())

        install.ensure_stop_hook(settings, rendered)

        after = _read(settings)
        assert len(_speak_hooks(after)) == 1, "missing speak hook should be merged back in"
        assert "UserPromptSubmit" in after["hooks"], "existing hooks preserved"
        assert after["model"] == "claude-opus-4-8", "unrelated keys preserved"


def test_ensure_stop_hook_skips_when_present():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        settings = proj / ".claude" / "settings.json"
        _write(settings, _settings_with_voice())
        # Rendered template has a *different* voice; no-clobber means we keep
        # what's there rather than appending a second hook.
        rendered = json.dumps({
            "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command",
                      "command": r'py "$CLAUDE_PROJECT_DIR\scripts\speak_lang.py" --voice de-DE-ConradNeural --tag de'}]}]},
        })
        install.ensure_stop_hook(settings, rendered)
        hooks = _speak_hooks(_read(settings))
        assert len(hooks) == 1 and "en-US-JennyNeural" in hooks[0]["command"]


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
