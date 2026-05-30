"""claude-speech voice-output toggle.

The TTS Stop hook in a project's .claude/settings.json fires on every reply and
reads aloud whatever Claude wrapped in the language tags. There is no runtime
"skill is active" state in Claude Code, so the only honest on/off switch for
spoken output is the presence of that hook. This script flips it:

  --off : remove the speak_lang.py Stop hook from settings.json and stash an
          exact copy in .claude/speak_lang.hook.json, so it can be restored
          later without re-running the whole installer (no need to re-choose
          language / voice / audio device).
  --on  : merge the stashed hook back into settings.json and delete the stash.

It is surgical: only Stop hooks whose command references speak_lang.py are
touched. Other Stop hooks, the UserPromptSubmit hook, and every other settings
key are preserved. Both modes are idempotent — running --off twice, or --on
with nothing stashed, is a harmless no-op that reports what it found.

Usage:
    py toggle_voice.py --project-dir <dir> --off
    py toggle_voice.py --project-dir <dir> --on
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Marker that identifies *our* Stop hook among any others the user may have.
HOOK_MARKER = "speak_lang.py"
STASH_NAME = "speak_lang.hook.json"


def resolve_project_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def is_speak_hook(hook: dict) -> bool:
    cmd = hook.get("command")
    return isinstance(cmd, str) and HOOK_MARKER in cmd


def load_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not parse {path}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def dump_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def turn_off(project_dir: Path) -> int:
    settings_path = project_dir / ".claude" / "settings.json"
    stash_path = project_dir / ".claude" / STASH_NAME

    settings = load_json(settings_path)
    if settings is None:
        print(f"voice output: no settings.json at {settings_path} — nothing to disable.")
        return 0

    stop_groups = (settings.get("hooks") or {}).get("Stop") or []
    kept_groups: list[dict] = []
    removed: list[dict] = []  # [{"matcher": str, "hooks": [hook, ...]}]

    for group in stop_groups:
        hooks = group.get("hooks", []) or []
        speak = [h for h in hooks if is_speak_hook(h)]
        rest = [h for h in hooks if not is_speak_hook(h)]
        if speak:
            removed.append({"matcher": group.get("matcher", ""), "hooks": speak})
        if rest:
            kept = dict(group)
            kept["hooks"] = rest
            kept_groups.append(kept)

    if not removed:
        if stash_path.exists():
            print("voice output: already off (no speak hook in settings.json; stash present).")
        else:
            print("voice output: no speak_lang Stop hook found — nothing to disable.")
        return 0

    # Rewrite Stop (drop the key entirely if nothing else is left there).
    if kept_groups:
        settings["hooks"]["Stop"] = kept_groups
    else:
        settings["hooks"].pop("Stop", None)
        if not settings["hooks"]:
            settings.pop("hooks", None)

    dump_json(stash_path, removed)
    dump_json(settings_path, settings)
    print(f"voice output: OFF. Stashed hook -> {stash_path}")
    return 0


def turn_on(project_dir: Path) -> int:
    settings_path = project_dir / ".claude" / "settings.json"
    stash_path = project_dir / ".claude" / STASH_NAME

    stashed = load_json(stash_path)
    if stashed is None:
        print("voice output: nothing stashed — already on, or it was never disabled here.")
        return 0

    settings = load_json(settings_path) or {}
    hooks_root = settings.setdefault("hooks", {})
    stop_groups = hooks_root.setdefault("Stop", [])

    def existing_commands() -> set[str]:
        cmds: set[str] = set()
        for group in stop_groups:
            for h in group.get("hooks", []) or []:
                if isinstance(h.get("command"), str):
                    cmds.add(h["command"])
        return cmds

    restored = 0
    for entry in stashed:
        matcher = entry.get("matcher", "")
        present = existing_commands()
        new_hooks = [h for h in entry.get("hooks", []) if h.get("command") not in present]
        if not new_hooks:
            continue
        # Reuse a Stop group with the same matcher if one exists; else add one.
        target = next((g for g in stop_groups if g.get("matcher", "") == matcher), None)
        if target is None:
            stop_groups.append({"matcher": matcher, "hooks": new_hooks})
        else:
            target.setdefault("hooks", []).extend(new_hooks)
        restored += len(new_hooks)

    dump_json(settings_path, settings)
    stash_path.unlink(missing_ok=True)
    if restored:
        print(f"voice output: ON. Restored {restored} hook(s) into {settings_path}")
    else:
        print("voice output: hook already present; cleared stale stash.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="claude-speech voice-output toggle")
    parser.add_argument("--project-dir", dest="project_dir",
                        help="project to toggle (default: $CLAUDE_PROJECT_DIR or CWD)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--off", action="store_true", help="remove + stash the speak_lang Stop hook")
    mode.add_argument("--on", action="store_true", help="restore the stashed speak_lang Stop hook")
    args = parser.parse_args(argv)

    project_dir = resolve_project_dir(args.project_dir)
    return turn_off(project_dir) if args.off else turn_on(project_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
