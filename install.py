"""claude-speech installer.

Scaffolds a language-tutor setup into a target project directory:
- CLAUDE.md                     : teacher persona for the chosen language
- .claude/settings.json         : Stop hook + UserPromptSubmit hook
- scripts/speak_lang.py         : Stop-hook TTS script (Claude's reply read aloud)
- scripts/push_to_talk.py       : push-to-talk daemon (record → Whisper → IPA → auto-submit)
- scripts/inject_transcript.py  : UserPromptSubmit hook (fallback path when auto-submit can't focus the chat window)

Project dir resolution order:
  1. --project-dir argument
  2. $CLAUDE_PROJECT_DIR environment variable
  3. current working directory

Usage:
    py install.py --target Dutch --common Russian
    py install.py --target German --common Russian --voice de-DE-ConradNeural
    py install.py --target Dutch --common Russian --project-dir D:\\Data\\Claude-TTS --force
    py install.py --target Dutch --common Russian --no-voice-in   # TTS-only, skip voice-in

Note: --target is the target LANGUAGE (the daemon uses the same name); the
scaffold destination is --project-dir. --lang is accepted as a hidden alias
for --target for backward compatibility.
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

# Single source of truth for a project's language setup. Written alongside
# CLAUDE.md (from the same install run, so the two can't disagree) and read back
# by push_to_talk.py when it is launched without explicit --target/--common/
# --input-device/hotkeys. That coupling is what keeps the teacher persona and the
# voice-in daemon from drifting apart (e.g. persona says English, mic transcribes
# Russian). Lives under .claude/ next to settings.json.
CONFIG_NAME = "claude_speech.json"

# Daemon hotkey defaults, mirrored here so the config records concrete keys even
# when the user kept the defaults. Must match push_to_talk.py's DEFAULT_*_HOTKEY.
DEFAULT_TARGET_HOTKEY = "f9"
DEFAULT_COMMON_HOTKEY = "f10"

# Selection-toolbar default scope (must match selection_toolbar.DEFAULT_TOOLBAR_WINDOW_RE):
# Claude-only unless the user opts into "everywhere".
DEFAULT_TOOLBAR_WINDOW_RE = r".*Claude.*"


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


def quote_interpreter_for_json(exe: str) -> str:
    """Return a Python interpreter path as a JSON-escaped, double-quoted token.

    The Stop/UserPromptSubmit hook commands live inside a JSON string in
    settings.json, so backslashes are doubled and the surrounding quotes are
    backslash-escaped (matching the template's \\"...\\" style). Quoting also
    handles spaces in the path (e.g. 'C:\\Program Files\\...').

    Baking the *real* interpreter (sys.executable) into the hook — instead of a
    bare ``py`` — avoids a silent-failure mode: if ``py`` isn't on PATH when
    Claude Code runs the hook, TTS just never plays, with no error surfaced.
    Using the same interpreter that ran the installer also guarantees the hook
    sees the deps install.py pip-installed (edge-tts, miniaudio, ...).
    """
    return '\\"' + exe.replace("\\", "\\\\") + '\\"'


def build_config(
    *, target: str, target_code: str, common: str, common_code: str, voice: str,
    input_device: str | None, output_device: str | None,
    target_hotkey: str, common_hotkey: str,
    selection_toolbar: bool = True, toolbar_window_re: str | None = DEFAULT_TOOLBAR_WINDOW_RE,
) -> dict:
    """Assemble the claude_speech.json payload — the single source of truth the
    push-to-talk daemon and selection toolbar read when not told explicitly.
    `selection_toolbar` gates whether the toolbar is launched; `toolbar_window_re`
    is its scope (Claude-only by default, or null for any application). Kept as a
    pure function so it can be unit-tested without touching the filesystem."""
    return {
        "target": target,
        "target_code": target_code,
        "common": common,
        "common_code": common_code,
        "voice": voice,
        "input_device": input_device,
        "output_device": output_device,
        "target_hotkey": target_hotkey,
        "common_hotkey": common_hotkey,
        "selection_toolbar": selection_toolbar,
        "toolbar_window_re": toolbar_window_re,
    }


def write_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"  wrote: {path}")


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


def ensure_stop_hook(settings_path: Path, rendered_settings_text: str) -> None:
    """When settings.json already exists and we don't overwrite it, make sure the
    live speak_lang Stop hook is actually present.

    This matters because `/claude-speech off` removes that hook (stashing a copy).
    An install invoked with language args clearly means the user wants the tutor
    set up, so leaving spoken output muted would be surprising. If the hook is
    absent we merge the freshly-rendered one in (preserving every other key); if
    a speak_lang hook is already there we leave it untouched — changing its voice
    or device is a --force operation, by the same no-clobber rule as the rest of
    the installer."""
    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  WARNING: could not parse {settings_path} to ensure Stop hook: {exc}", file=sys.stderr)
        return

    existing_stop = (existing.get("hooks") or {}).get("Stop") or []
    has_speak = any(
        isinstance(h.get("command"), str) and "speak_lang.py" in h["command"]
        for group in existing_stop
        for h in (group.get("hooks") or [])
    )
    if has_speak:
        return  # voice already on — respect no-clobber (use --force to change it)

    desired_stop = (json.loads(rendered_settings_text).get("hooks") or {}).get("Stop") or []
    if not desired_stop:
        return

    hooks_root = existing.setdefault("hooks", {})
    hooks_root.setdefault("Stop", []).extend(desired_stop)
    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"  re-added speak_lang Stop hook to existing {settings_path}")


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
        "argostranslate": "argostranslate",
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
VOICE_IN_DEPS = ["numpy", "sounddevice", "scipy", "pynput", "pywinauto", "pyperclip", "argostranslate"]

# Required only when TTS plays to a chosen output device (speak_lang.py
# --output-device): miniaudio decodes the edge-tts MP3 so sounddevice can play
# it on a specific endpoint. numpy/sounddevice come from VOICE_IN_DEPS; with
# --no-voice-in we still need them for the chosen-device playback path.
OUTPUT_DEVICE_DEPS = ["miniaudio", "numpy", "sounddevice"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="claude-speech installer")
    parser.add_argument("--target", help="target language being learned: name (e.g. Dutch) or ISO 639-1 code (e.g. nl)")
    parser.add_argument("--lang", help=argparse.SUPPRESS)  # back-compat alias for --target
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
    parser.add_argument("--target-hotkey", help=f"push-to-talk key for the target language, recorded in {CONFIG_NAME} (default: {DEFAULT_TARGET_HOTKEY})")
    parser.add_argument("--common-hotkey", help=f"push-to-talk key for the common language, recorded in {CONFIG_NAME} (default: {DEFAULT_COMMON_HOTKEY})")
    parser.add_argument("--no-selection-toolbar", action="store_true",
                        help=f"disable the select-text-to-read/translate toolbar (recorded in {CONFIG_NAME}; default: enabled)")
    parser.add_argument("--toolbar-everywhere", action="store_true",
                        help="let the selection toolbar work in any application (default: only inside the Claude app)")
    parser.add_argument("--project-dir", dest="project_dir", help="project directory to scaffold into (default: $CLAUDE_PROJECT_DIR or CWD)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--skip-pip", action="store_true", help="don't run any pip installs (TTS or voice-in)")
    parser.add_argument(
        "--no-voice-in",
        action="store_true",
        help="skip the voice-in pipeline (push_to_talk.py, inject_transcript.py, voice-in pip deps). TTS Stop hook is still set up.",
    )
    parser.add_argument(
        "--gpu",
        choices=["auto", "cpu", "cuda", "vulkan"],
        help="after scaffold, provision the whisper.cpp backend for the detected/chosen "
             "GPU (delegates to provision_whisper.py): auto detects, or force cpu/cuda/vulkan. "
             "Ignored with --no-voice-in. "
             "Runs immediately; preview the plan first with: "
             "py provision_whisper.py --project-dir <dir> --gpu auto --detect-only.",
    )
    args = parser.parse_args(argv)

    voices = load_voices()
    available = ", ".join(f"{v['name']} ({v['code']})" for v in voices)

    target_lang = args.target or args.lang
    if not target_lang:
        print("ERROR: no target language given; pass --target <name|code> (e.g. --target Dutch).", file=sys.stderr)
        return 2

    entry = find_language(voices, target_lang)
    if entry is None:
        print(f"ERROR: unknown target language '{target_lang}'.\nAvailable: {available}", file=sys.stderr)
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

    project_dir = resolve_target(args.project_dir)
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
        "TARGET": str(project_dir).replace("\\", "\\\\"),  # JSON-safe path
        # Bake the real interpreter into the hook commands rather than a bare
        # `py` (which fails silently if not on PATH when the hook runs).
        "PY": quote_interpreter_for_json(sys.executable),
    }

    print(
        f"Scaffolding target {entry['name']} ({entry['code']}, voice {voice}) "
        f"+ common {common_entry['name']} ({common_entry['code']}) into:\n  {project_dir}\n"
    )
    project_dir.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md_tpl = (TPL_DIR / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
    wrote_claude = write_file(project_dir / "CLAUDE.md", render(claude_md_tpl, mapping), args.force)

    # .claude/claude_speech.json — single source of truth for the daemon, written
    # in lockstep with CLAUDE.md so persona and voice-in can't drift apart. Refresh
    # it whenever CLAUDE.md is (re)written, and backfill it for pre-existing
    # installs that predate this file (so an old project gains one on next run).
    config_path = project_dir / ".claude" / CONFIG_NAME
    if wrote_claude or not config_path.exists():
        write_config(config_path, build_config(
            target=entry["name"], target_code=entry["code"],
            common=common_entry["name"], common_code=common_entry["code"],
            voice=voice,
            input_device=args.input_device,
            output_device=args.output_device,
            target_hotkey=args.target_hotkey or DEFAULT_TARGET_HOTKEY,
            common_hotkey=args.common_hotkey or DEFAULT_COMMON_HOTKEY,
            selection_toolbar=not args.no_selection_toolbar,
            toolbar_window_re=(None if args.toolbar_everywhere else DEFAULT_TOOLBAR_WINDOW_RE),
        ))

    # .claude/settings.json
    settings_tpl = (TPL_DIR / "settings.json.tmpl").read_text(encoding="utf-8")
    settings_path = project_dir / ".claude" / "settings.json"
    rendered_settings = render(settings_tpl, mapping)
    wrote_settings = write_file(settings_path, rendered_settings, args.force)
    if not wrote_settings and settings_path.exists():
        # Existing file we didn't overwrite: guarantee the Stop hook is present
        # (it may have been removed by `/claude-speech off`), then sanity-check it.
        ensure_stop_hook(settings_path, rendered_settings)
        validate_existing_settings(settings_path, project_dir)
    # Any install invoked with language args supersedes a prior `/claude-speech off`,
    # so drop the stash regardless of whether we wrote or merged — the live hook is
    # now in settings.json and the stash would only desync the on/off toggle.
    stash = project_dir / ".claude" / "speak_lang.hook.json"
    if stash.exists():
        stash.unlink()
        print(f"  cleared voice-off stash (superseded by install): {stash}")

    # scripts/ — copy each script verbatim (no template substitutions)
    # cs_common.py is a base dependency: speak_lang.py (the TTS Stop hook) imports
    # it, so it ships even in --no-voice-in (TTS-only) installs.
    scripts_to_copy = ["speak_lang.py", "cs_common.py"]
    if not args.no_voice_in:
        scripts_to_copy.extend(["push_to_talk.py", "inject_transcript.py"])
        if not args.no_selection_toolbar:
            scripts_to_copy.append("selection_toolbar.py")

    for name in scripts_to_copy:
        script_src = TPL_DIR / "scripts" / name
        script_dst = project_dir / "scripts" / name
        if script_dst.exists() and not args.force:
            print(f"  skip (exists): {script_dst}  [use --force to overwrite]")
        else:
            script_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(script_src, script_dst)
            print(f"  wrote: {script_dst}")

    # logs/ — pre-create so the scripts don't race on first run
    (project_dir / "logs").mkdir(parents=True, exist_ok=True)
    # recordings/ — pre-create for push_to_talk.py (no-op if --no-voice-in)
    if not args.no_voice_in:
        (project_dir / "recordings").mkdir(parents=True, exist_ok=True)

    if not args.skip_pip:
        ensure_pip_packages(TTS_DEPS, "TTS deps")
        if not args.no_voice_in:
            ensure_pip_packages(VOICE_IN_DEPS, "Voice-in deps")
        # Playing TTS on a chosen output device decodes MP3 via miniaudio +
        # sounddevice; only needed when --output-device was requested.
        if args.output_device:
            ensure_pip_packages(OUTPUT_DEVICE_DEPS, "Output-device deps")

    print(
        f"\nDone. Open '{project_dir}' in Claude Code and say hi — the assistant will greet you in "
        f"{entry['name']} and write its notes in {common_entry['name']}."
    )
    print(
        "\nIMPORTANT: Claude Code loads hooks at session start. If you ran this from\n"
        "inside the session you'll be chatting in, RESTART Claude Code (or reload\n"
        "config) and APPROVE the new Stop hook when prompted — otherwise spoken\n"
        "output stays silent because the hook was never loaded."
    )
    if not args.no_voice_in:
        if args.gpu:
            import provision_whisper
            rc = provision_whisper.main(["--project-dir", str(project_dir), "--gpu", args.gpu])
            if rc != 0:
                return rc
        else:
            print(
                "\nVoice-in scaffolded. Before you can use F9 push-to-talk you must also\n"
                "install the binary dependencies (NOT shipped via pip):\n"
                "  - whisper.cpp        -> tools/whisper.cpp/bin/Release/whisper-server.exe\n"
                "  - whisper model      -> tools/whisper.cpp/models/ggml-medium-q5_0.bin (or similar)\n"
                "  - espeak-ng (IPA)    -> tools/espeak-ng/espeak-ng.exe\n"
                "Or run automatically: py install.py ... --gpu auto (detects your card).\n"
                "See the README's 'Voice input — binary dependencies' section for exact commands."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
