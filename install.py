"""claude-speech installer.

Scaffolds a language-tutor setup into a target project directory:
- CLAUDE.md         : teacher persona for the chosen language
- .claude/settings.json : Stop hook wired to speak_lang.py
- scripts/speak_lang.py : TTS script

Target dir resolution order:
  1. --target argument
  2. $CLAUDE_PROJECT_DIR environment variable
  3. current working directory

Usage:
    py install.py --lang Dutch
    py install.py --lang German --voice de-DE-ConradNeural
    py install.py --lang Dutch --target D:\\Data\\Claude-TTS --force
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VOICES_PATH = HERE / "voices.json"
TPL_DIR = HERE / "templates"


def load_voices() -> list[dict]:
    with VOICES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def find_language(voices: list[dict], name: str) -> dict | None:
    needle = name.strip().lower()
    for entry in voices:
        if entry["name"].lower() == needle:
            return entry
        if entry.get("code", "").lower() == needle:
            return entry
    return None


def resolve_target(arg_target: str | None) -> Path:
    if arg_target:
        return Path(arg_target).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def render(tpl_text: str, mapping: dict[str, str]) -> str:
    out = tpl_text
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def write_file(path: Path, content: str, force: bool) -> bool:
    """Returns True if written, False if skipped (existed and not force)."""
    if path.exists() and not force:
        print(f"  skip (exists): {path}  [use --force to overwrite]")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"  wrote: {path}")
    return True


def ensure_edge_tts() -> None:
    if importlib.util.find_spec("edge_tts") is not None:
        print("edge-tts already installed.")
        return
    print("edge-tts not found — installing via pip --user...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "edge-tts"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="claude-speech installer")
    parser.add_argument("--lang", required=True, help="language name (e.g. Dutch, German) or ISO 639-1 code (e.g. nl)")
    parser.add_argument("--voice", help="override edge-tts voice id (otherwise uses the recommended one from voices.json)")
    parser.add_argument("--target", help="target project directory (default: $CLAUDE_PROJECT_DIR or CWD)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--skip-pip", action="store_true", help="don't try to install edge-tts")
    args = parser.parse_args(argv)

    voices = load_voices()
    entry = find_language(voices, args.lang)
    if entry is None:
        available = ", ".join(f"{v['name']} ({v['code']})" for v in voices)
        print(f"ERROR: unknown language '{args.lang}'.\nAvailable: {available}", file=sys.stderr)
        return 2

    target = resolve_target(args.target)
    voice = args.voice or entry["voice"]

    mapping = {
        "LANG_NAME": entry["name"],
        "LANG_CODE": entry["code"],
        "ISO": entry["iso"],
        "VOICE": voice,
        "TARGET": str(target).replace("\\", "\\\\"),  # JSON-safe path
    }

    print(f"Scaffolding {entry['name']} ({entry['code']}, voice {voice}) into:\n  {target}\n")
    target.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md_tpl = (TPL_DIR / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
    write_file(target / "CLAUDE.md", render(claude_md_tpl, mapping), args.force)

    # .claude/settings.json
    settings_tpl = (TPL_DIR / "settings.json.tmpl").read_text(encoding="utf-8")
    write_file(target / ".claude" / "settings.json", render(settings_tpl, mapping), args.force)

    # scripts/speak_lang.py — copy verbatim (no substitutions)
    script_src = TPL_DIR / "scripts" / "speak_lang.py"
    script_dst = target / "scripts" / "speak_lang.py"
    if script_dst.exists() and not args.force:
        print(f"  skip (exists): {script_dst}  [use --force to overwrite]")
    else:
        script_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script_src, script_dst)
        print(f"  wrote: {script_dst}")

    # logs/ — pre-create so the script doesn't race on first run
    (target / "logs").mkdir(parents=True, exist_ok=True)

    if not args.skip_pip:
        ensure_edge_tts()

    print(f"\nDone. Open '{target}' in Claude Code and say hi — the assistant will greet you in {entry['name']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
