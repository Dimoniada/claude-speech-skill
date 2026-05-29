"""claude-speech installer.

Scaffolds a language-tutor setup into a target project directory:
- CLAUDE.md                     : teacher persona for the chosen language
- .claude/settings.json         : Stop hook + UserPromptSubmit hook
- scripts/speak_lang.py         : Stop-hook TTS script (Claude's reply read aloud)
- scripts/push_to_talk.py       : push-to-talk daemon (record → Whisper → IPA → auto-submit)
- scripts/inject_transcript.py  : UserPromptSubmit hook (fallback path when auto-submit can't focus the chat window)

Target dir resolution order:
  1. --target argument
  2. $CLAUDE_PROJECT_DIR environment variable
  3. current working directory

Usage:
    py install.py --lang Dutch --common Russian
    py install.py --lang German --common Russian --voice de-DE-ConradNeural
    py install.py --lang Dutch --common Russian --target D:\\Data\\Claude-TTS --force
    py install.py --lang Dutch --common Russian --no-voice-in   # TTS-only, skip voice-in
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


def validate_existing_settings(path: Path, target: Path) -> None:
    """If we skipped writing settings.json, check the existing file's Stop-hook
    command actually points at this install target. Warn loudly otherwise —
    a stale absolute path here is the #1 silent-failure mode (no log, no audio,
    no error)."""
    try:
        with path.open(encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARNING: could not parse existing {path}: {exc}", file=sys.stderr)
        return

    stop_hooks = (settings.get("hooks") or {}).get("Stop") or []
    commands: list[str] = []
    for group in stop_hooks:
        for hook in group.get("hooks", []) or []:
            cmd = hook.get("command")
            if isinstance(cmd, str):
                commands.append(cmd)

    if not commands:
        return  # nothing to validate

    target_str = str(target)
    target_str_escaped = target_str.replace("\\", "\\\\")
    for cmd in commands:
        if "$CLAUDE_PROJECT_DIR" in cmd or "${CLAUDE_PROJECT_DIR}" in cmd:
            continue  # portable, fine
        if target_str in cmd or target_str_escaped in cmd:
            continue  # absolute path matches this target
        print(
            "  WARNING: existing Stop-hook command does not reference this install target.\n"
            f"           command : {cmd}\n"
            f"           target  : {target_str}\n"
            "           This will silently fail to play audio. Re-run with --force "
            "or fix the path in .claude/settings.json by hand.",
            file=sys.stderr,
        )


def ensure_pip_packages(packages: list[str], group_label: str) -> None:
    """pip-install any of `packages` that aren't already importable.

    `packages` are pip distribution names. We map them to import names below
    where they differ (e.g. pip name 'pywin32' would have import name 'win32api').
    """
    pip_to_import = {
        # pip name -> module name used for find_spec
        "edge-tts": "edge_tts",
        "sounddevice": "sounddevice",
        "scipy": "scipy",
        "numpy": "numpy",
        "pynput": "pynput",
        "pywinauto": "pywinauto",
        "pyperclip": "pyperclip",
        "miniaudio": "miniaudio",
    }
    missing = []
    for pkg in packages:
        mod = pip_to_import.get(pkg, pkg.replace("-", "_"))
        if importlib.util.find_spec(mod) is None:
            missing.append(pkg)

    if not missing:
        print(f"{group_label}: already installed ({', '.join(packages)}).")
        return

    print(f"{group_label}: installing {', '.join(missing)} via pip --user ...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--user", *missing]
    )


# Packages required by the TTS Stop hook (speak_lang.py).
TTS_DEPS = ["edge-tts"]

# Packages required by the voice-in pipeline (push_to_talk.py + inject_transcript.py).
# Note: whisper.cpp, the ggml model, and espeak-ng are binary deps — NOT pip
# installable. The README documents how to provision them manually. This list
# is only the Python side.
VOICE_IN_DEPS = ["numpy", "sounddevice", "scipy", "pynput", "pywinauto", "pyperclip"]

# Required only when TTS plays to a chosen output device (speak_lang.py
# --output-device): miniaudio decodes the edge-tts MP3 so sounddevice can play
# it on a specific endpoint. numpy/sounddevice come from VOICE_IN_DEPS; with
# --no-voice-in we still need them for the chosen-device playback path.
OUTPUT_DEVICE_DEPS = ["miniaudio", "numpy", "sounddevice"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="claude-speech installer")
    parser.add_argument("--lang", required=True, help="target language being learned: name (e.g. Dutch) or ISO 639-1 code (e.g. nl)")
    parser.add_argument("--common", required=True, help="communication language for notes/corrections: name (e.g. Russian) or ISO 639-1 code (e.g. ru)")
    parser.add_argument("--voice", help="override edge-tts voice id (otherwise uses the recommended one from voices.json)")
    parser.add_argument(
        "--input-device",
        help="microphone for push-to-talk: device index or a name substring. Baked into the daemon launch hint; the daemon also accepts --input-device directly. List options with: py scripts/push_to_talk.py --list-devices",
    )
    parser.add_argument(
        "--output-device",
        help="speaker/headphone for TTS playback: device index or a name substring. Baked into the Stop hook in settings.json. List options with: py scripts/speak_lang.py --list-devices",
    )
    parser.add_argument("--target", help="target project directory (default: $CLAUDE_PROJECT_DIR or CWD)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--skip-pip", action="store_true", help="don't run any pip installs (TTS or voice-in)")
    parser.add_argument(
        "--no-voice-in",
        action="store_true",
        help="skip the voice-in pipeline (push_to_talk.py, inject_transcript.py, voice-in pip deps). TTS Stop hook is still set up.",
    )
    args = parser.parse_args(argv)

    voices = load_voices()
    available = ", ".join(f"{v['name']} ({v['code']})" for v in voices)

    entry = find_language(voices, args.lang)
    if entry is None:
        print(f"ERROR: unknown target language '{args.lang}'.\nAvailable: {available}", file=sys.stderr)
        return 2

    common_entry = find_language(voices, args.common)
    if common_entry is None:
        print(f"ERROR: unknown common language '{args.common}'.\nAvailable: {available}", file=sys.stderr)
        return 2

    if common_entry["code"] == entry["code"]:
        print(
            f"ERROR: target and common language must differ (both are {entry['name']}).",
            file=sys.stderr,
        )
        return 2

    target = resolve_target(args.target)
    voice = args.voice or entry["voice"]

    # Bake the chosen output device into the Stop-hook command in settings.json.
    # The value lands inside a JSON string, so the quotes around the (possibly
    # space-containing) device name must be backslash-escaped to match the
    # template's existing \"...\" escaping. Empty when no device was chosen.
    if args.output_device:
        output_device_arg = ' --output-device \\"' + args.output_device.replace('"', "") + '\\"'
    else:
        output_device_arg = ""

    mapping = {
        "LANG_NAME": entry["name"],
        "LANG_CODE": entry["code"],
        "ISO": entry["iso"],
        "VOICE": voice,
        "COMMON_NAME": common_entry["name"],
        "COMMON_CODE": common_entry["code"],
        "COMMON_ISO": common_entry["iso"],
        "OUTPUT_DEVICE_ARG": output_device_arg,
        "TARGET": str(target).replace("\\", "\\\\"),  # JSON-safe path
    }

    print(
        f"Scaffolding target {entry['name']} ({entry['code']}, voice {voice}) "
        f"+ common {common_entry['name']} ({common_entry['code']}) into:\n  {target}\n"
    )
    target.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md_tpl = (TPL_DIR / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
    write_file(target / "CLAUDE.md", render(claude_md_tpl, mapping), args.force)

    # .claude/settings.json
    settings_tpl = (TPL_DIR / "settings.json.tmpl").read_text(encoding="utf-8")
    settings_path = target / ".claude" / "settings.json"
    wrote_settings = write_file(settings_path, render(settings_tpl, mapping), args.force)
    if not wrote_settings and settings_path.exists():
        validate_existing_settings(settings_path, target)

    # scripts/ — copy each script verbatim (no template substitutions)
    scripts_to_copy = ["speak_lang.py"]
    if not args.no_voice_in:
        scripts_to_copy.extend(["push_to_talk.py", "inject_transcript.py"])

    for name in scripts_to_copy:
        script_src = TPL_DIR / "scripts" / name
        script_dst = target / "scripts" / name
        if script_dst.exists() and not args.force:
            print(f"  skip (exists): {script_dst}  [use --force to overwrite]")
        else:
            script_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(script_src, script_dst)
            print(f"  wrote: {script_dst}")

    # logs/ — pre-create so the scripts don't race on first run
    (target / "logs").mkdir(parents=True, exist_ok=True)
    # recordings/ — pre-create for push_to_talk.py (no-op if --no-voice-in)
    if not args.no_voice_in:
        (target / "recordings").mkdir(parents=True, exist_ok=True)

    if not args.skip_pip:
        ensure_pip_packages(TTS_DEPS, "TTS deps")
        if not args.no_voice_in:
            ensure_pip_packages(VOICE_IN_DEPS, "Voice-in deps")
        # Playing TTS on a chosen output device decodes MP3 via miniaudio +
        # sounddevice; only needed when --output-device was requested.
        if args.output_device:
            ensure_pip_packages(OUTPUT_DEVICE_DEPS, "Output-device deps")

    print(
        f"\nDone. Open '{target}' in Claude Code and say hi — the assistant will greet you in "
        f"{entry['name']} and write its notes in {common_entry['name']}."
    )
    if not args.no_voice_in:
        print(
            "\nVoice-in scaffolded. Before you can use F9 push-to-talk you must also\n"
            "install the binary dependencies (NOT shipped via pip):\n"
            "  - whisper.cpp        -> tools/whisper.cpp/bin/Release/whisper-cli.exe\n"
            "  - whisper model      -> tools/whisper.cpp/models/ggml-medium-q5_0.bin (or similar)\n"
            "  - espeak-ng (IPA)    -> tools/espeak-ng/espeak-ng.exe\n"
            "See the README's 'Voice input — binary dependencies' section for exact commands."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
