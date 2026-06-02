---
name: claude-speech
description: Scaffold a two-language tutor in any project — Claude speaks the target language aloud (with IPA) while notes and corrections in your native language stay silent. Use when the user asks to learn or practice a foreign language with spoken feedback, or when they want Claude's target-language responses read aloud in Claude Code.
---

# claude-speech

This skill bootstraps a self-contained language-learning project inside the user's current Claude Code workspace. It works with **two languages**: a **target language** (the one being learned — spoken aloud, with IPA pronunciation help) and a **common language** (the learner's native tongue — used for notes, corrections, and free chat, never spoken). It installs:
- a teacher persona (`CLAUDE.md`) that speaks the target language and writes all notes in the common language,
- a `Stop` hook + `scripts/speak_lang.py` that uses `edge-tts` to speak only the target-language portion of Claude's replies aloud,
- a `UserPromptSubmit` hook + `scripts/push_to_talk.py` + `scripts/inject_transcript.py` for **two-key push-to-talk voice input** — hold **F9** to speak the target language or **F10** to speak the common language. The held key forces the transcription language (no auto-detection, so mixed-language speech isn't misread), transcribes via local Whisper, adds an IPA line (espeak-ng) only for target-language speech, and pastes the result into the chat as your message.

## When to use

Trigger when the user says any of:
- "let's practice {language}"
- "teach me {language}"
- "set up a {language} tutor"
- "I want Claude to speak {language} responses"
- "scaffold claude-speech for {language}"

## How to invoke

0. **Handle control arguments first.** Before any of the steps below, inspect the argument the user passed:
   - If the argument is `off`, `stop`, or `kill` (case-insensitive): skip all install and scaffold steps. Take these actions in order:
     1. Find and terminate every running `push_to_talk.py` daemon. On Windows the reliable command is (note the **single-quoted** `-Command` argument so this also works when invoked from bash/zsh — double quotes would let the shell interpolate `$_` and `$(...)` before PowerShell sees them):
        ```
        powershell -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object { $_.Name -in @("py.exe","python.exe","pythonw.exe") -and $_.CommandLine -like "*push_to_talk.py*" } | ForEach-Object { Write-Host "killed PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }'
        ```
        **Crucially: the filter MUST require the process Name to be one of `py.exe` / `python.exe` / `pythonw.exe`.** Without that restriction, the filter also matches shell wrappers that happen to have `push_to_talk.py` literally in their command line (e.g. the very PowerShell invocation you're running) and will pollute the result list.
     2. Terminate the resident `whisper-server.exe` the daemon started (it loads the model into VRAM and does **not** die with the daemon when force-killed). Match only **this project's** server by requiring its command line to reference the project's `tools\whisper.cpp` path, so other projects' servers are left running:
        ```
        powershell -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "whisper-server.exe" -and $_.CommandLine -like "*<project_root>\tools\whisper.cpp*" } | ForEach-Object { Write-Host "killed server PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }'
        ```
        Substitute `<project_root>` with the actual project directory. (If voice-in was never set up, or the daemon never started, the filter simply matches nothing.)
     3. Delete `<project_root>/recordings/latest_transcript.txt` if it exists, so the UserPromptSubmit hook doesn't keep re-injecting the last transcript on subsequent manual Enters now that the daemon isn't writing fresh ones.
     4. **Disable spoken output.** Run `py toggle_voice.py --project-dir <project_root> --off` (from this skill's directory). This is what actually silences replies: it surgically removes the `speak_lang.py` Stop hook from `.claude/settings.json` and stashes an exact copy in `.claude/speak_lang.hook.json` so it can be restored later without re-running the installer. The mic daemon (steps 1–2) and spoken output are independent switches — killing the daemon alone leaves the Stop hook firing, so this step is required for a full "off". It's idempotent: a no-op if voice was already off.
     5. Report the PIDs killed (or "no daemons were running"), confirm the stale transcript was cleared, and confirm spoken output is now off.
   - **If no language argument was given AND `<project_root>/.claude/speak_lang.hook.json` exists** (case-insensitive check happens above first): this is a "turn voice back on" re-invocation, not a fresh setup. Skip the install/interview steps. Run `py toggle_voice.py --project-dir <project_root> --on` to merge the stashed Stop hook back into `settings.json` (it deletes the stash afterward), then restart the push-to-talk daemon per step 7 (only if `<project_root>/scripts/push_to_talk.py` exists). Report that spoken output is back on. This restores the previous language/voice/device with no re-interview — that's the whole point of the stash.
   - Any other argument is treated as a language name or ISO code — run a **full (re)initialization** via steps 1+ below. The user only ever types `/claude-speech [<target> <common>]` or `/claude-speech off`; **`--force` is an installer flag *you* (the assistant) decide to pass — never ask the user to type it.** Apply this rule:
     - **A scaffold already exists in this project** (any of `CLAUDE.md`, `.claude/settings.json`, `scripts/speak_lang.py` present): pass `--force` to `install.py` yourself. This regenerates `CLAUDE.md`, the scripts, and the Stop hook together, so the teacher persona and the hook always agree on the language — there is no half-updated state. This is mandatory when the requested language differs from the existing setup, and harmless when it's the same.
     - **No scaffold yet:** a normal install (no `--force` needed) writes everything fresh.
     - Either way the installer deletes any leftover `/claude-speech off` stash, so voice ends up *on* with the requested config.
   - **Reusing the previous setup instead of re-asking.** A full init still needs all required args (target, common, audio devices — steps 1, 3). Rather than making the user re-answer everything, first recover the previous values: read the prior `--target`/`--common`/`--voice` from the existing `CLAUDE.md`, the output device from the `speak_lang` command in `.claude/settings.json` (or the stash `.claude/speak_lang.hook.json` if voice is currently off), and the input device plus any `--target-hotkey`/`--common-hotkey` from the running daemon's command line if present. Offer these as defaults — "Previously: Dutch ← Russian, voice en-US-…, mic '…', speaker '…', keys F9/F10. Keep these, or change?" — and only prompt for what the user wants to change or what genuinely can't be recovered. Args the user passed on the command line always win over recovered defaults.

1. **Resolve the two languages.** The skill takes two positional arguments: `/claude-speech <target> <common>`.
   - **Arg 1 = target language** (the one being learned, spoken aloud + IPA).
   - **Arg 2 = common language** (the learner's native language, used for notes/corrections, never spoken).
   - Accept names ("Dutch", "Russian") or ISO 639-1 codes ("nl", "ru"). Both must exist in `voices.json` next to this skill — open it if the user asks what's available.
   - If the target (arg 1) is missing, ask which language to teach.
   - **If the common language (arg 2) is missing, ask for it before proceeding** — do not assume English.

2. **Ask if they want a non-default voice.** Each language has a recommended `edge-tts` voice; if the user wants something different (different gender, accent, or specific neural voice), they can pass it as `--voice <voice-id>`.

3. **Select audio devices — REQUIRED, before anything is installed or launched.** Device selection is mandatory: do not run the installer, do not write files, and do not spawn any background process until the user has explicitly chosen both an input and an output device. Do this as two separate, ordered choices:
   1. **List the devices.** Run `py templates/scripts/push_to_talk.py --list-devices` (works from the skill directory; it needs only the voice-in Python deps). It prints input devices first, then output devices, each with an index, name, and host API.
   2. **Microphone (input) — required.** Show the user the input-device list and ask which microphone to use. Wait for an explicit answer. If the user declines or gives no usable choice, **stop here** — report that a microphone is required and that nothing was installed or started. (Exception: if the user explicitly asked for a TTS-only setup with `--no-voice-in`, there is no push-to-talk, so skip the microphone step.)
   3. **Speaker (output) — required.** Then show the output-device list and ask which speaker/headphone to use for spoken replies. Wait for an explicit answer. If the user declines or gives no usable choice, **stop here** — report that an output device is required and that nothing was installed or started.
   - **Prefer a name substring** (e.g. `"USB PnP"`, `"OnePlus"`) over a raw index when recording the choice — indices are reassigned across reboots/replugs, names are stable. Pick a substring that is specific enough to identify the device the user named.
   - Carry the input choice into the daemon spawn (`--input-device`) in step 7 and the output choice into the installer (`--output-device`) in step 5.
   - To turn everything off later, the user runs `/claude-speech off` (or `stop` / `kill`) — see step 0.

3b. **Select CPU or GPU for voice-in — before install.** After devices and before running the installer, detect the GPU and let the user choose:
   1. Run `py provision_whisper.py --project-dir <dir> --gpu auto --detect-only`. (`<dir>` is resolved the same way as the project-dir step: `$CLAUDE_PROJECT_DIR` env var if set, otherwise CWD — so it is available here before the formal project-dir step.) It prints the detected GPU, the recommended backend (NVIDIA→CUDA, AMD/Intel→Vulkan, none→CPU), and a plan with sizes, rough time, and what is already installed.
   2. Show that plan and ask **CPU or GPU?**
      - **CPU** → pass `--gpu cpu` to the installer in step 5.
      - **GPU** → show the full plan, get **explicit consent**, then pass `--gpu auto` (or `cuda`/`vulkan`). Without consent, do not provision.
   - The Vulkan path (AMD/Intel) installs VS Build Tools + Vulkan SDK via winget and compiles from source — that is why explicit consent is required. NVIDIA/CPU are plain downloads. Already-installed dependencies are skipped. Any failure stops and rolls back in-project artifacts (system SDKs are kept).
   - Skip this step entirely with `--no-voice-in` (TTS-only).

3c. **Offer to remap the push-to-talk hotkeys — OPTIONAL, default F9/F10.** Unlike device selection (step 3), this is **not** a hard stop: the daemon defaults to **F9** (speak target) / **F10** (speak common), so if the user has no preference, keep the defaults and move on without blocking. Ask once, e.g.: "Push-to-talk uses **F9** to speak {target} and **F10** to speak {common} — keep these, or remap?"
   - If they want different keys, accept any single key name the daemon's `pynput` listener understands (function keys `f1`–`f12`, letters, etc.). Require the two keys to be **distinct** — if they pick the same key for both, ask again.
   - Carry the chosen keys into the daemon spawn (`--target-hotkey` / `--common-hotkey`) in step 7. If the defaults are kept, omit both flags (the daemon already defaults to F9/F10).
   - Skip this step entirely with `--no-voice-in` (there is no push-to-talk).

4. **Resolve the project directory.** Use `$CLAUDE_PROJECT_DIR` (the current Claude Code project root). If that env var is missing, fall back to the current working directory. Confirm the directory with the user before writing files.

5. **Run the installer** (from this skill's directory), passing the output device chosen in step 3:
   ```
   py install.py --target <target> --common <common> --output-device "<name|index>" --gpu <auto|cpu|cuda|vulkan> [--voice <voice-id>] [--project-dir <dir>] [--force] [--no-voice-in]
   ```
   Note: `--target` is the target **language** (same name the daemon uses) and `--common` is the communication language; the scaffold **destination** is `--project-dir`, not `--target`. (`--lang` is still accepted as a hidden alias for `--target`.) `--output-device` is the speaker chosen in step 3 (required by this skill's flow) and is baked into the Stop hook in `.claude/settings.json`. This writes `CLAUDE.md`, `.claude/settings.json`, `scripts/speak_lang.py`, `scripts/push_to_talk.py`, and `scripts/inject_transcript.py` into the project dir. It also pip-installs `edge-tts`, the voice-in deps (`numpy sounddevice scipy pynput pywinauto pyperclip`), and `miniaudio` (for output-device playback) if missing. Pass `--no-voice-in` to skip the push-to-talk pieces (TTS-only setup — then only the output device is needed). `--gpu` provisions the whisper.cpp backend after scaffold (delegates to `provision_whisper.py`); omit it to keep binaries bring-your-own.

6. **Confirm next steps** with the user: open the project dir in a fresh Claude Code session (or reload `/config` if already inside), then say hi — the assistant will greet them in the target language (with notes in the common language) using the agreed tag convention.

7. **Auto-start the push-to-talk daemon (if it exists in the project dir).** If `<project-dir>/scripts/push_to_talk.py` is present (i.e. the user didn't pass `--no-voice-in`), spawn it in the background with the chosen language code:
   - Before spawning, kill any prior instance to avoid duplicate keyboard listeners (use the same PowerShell command from step 0).
   - Spawn detached so it survives this turn. From Claude Code's Bash tool, use `run_in_background=true` with the microphone chosen in step 3 and the hotkeys chosen in step 3c: `py "<project-dir>\\scripts\\push_to_talk.py" --target <target_code> --common <common_code> --input-device "<name|index>" [--target-hotkey <key> --common-hotkey <key>]`. Omit the two hotkey flags when the user kept the F9/F10 defaults.
   - Confirm to the user that the daemon is running and which keys to hold: the **target-language key** (default **F9**, with IPA) and the **common-language key** (default **F10**, text only) — naming the actual keys if they were remapped in step 3c. Remind them they may need to set `--window-title-re` if the auto-submit picks the wrong window; tell them to run with `--list-windows` once to see candidates. The keys are configurable via `--target-hotkey` / `--common-hotkey`, and the microphone via `--input-device` (list options with `--list-devices`).
   - **Transcription backend:** the daemon auto-starts a resident `whisper-server` (bound to `127.0.0.1:8910`) that keeps the model + CUDA context warm in VRAM, so repeat transcriptions take well under a second instead of a ~3.5 s per-clip cold start. It is shut down when the daemon stops (or by `/claude-speech off`, per step 0). If port 8910 is already in use (another app, or another project's server), the daemon automatically probes upward and uses the next free port — no action needed; pass `--server-port <n>` only to pin a specific starting port. If the server still can't start (e.g. binaries are missing) the daemon exits with an actionable error.
   - **Important:** if you ran the GPU provisioning step (step 3b / `--gpu`), these binaries are already installed. Otherwise — or with `--no-voice-in` — they remain bring-your-own; see the README's "Voice input — binary dependencies" section.

## Tag convention

Generated `CLAUDE.md` instructs the assistant to wrap every target-language utterance in `<{code}>...</{code}>` tags (where `{code}` is the ISO 639-1 code: `<nl>`, `<de>`, `<es>`, etc.). The Stop hook extracts only that content and sends it to TTS. Anything outside the tags — pedagogical notes, corrections, and follow-up questions written in the common (native) language — stays silent and only appears as text.

## Adding a new language

Edit `voices.json`. Add an object with `name`, `code`, `iso`, `voice` fields. To find an `edge-tts` voice id, run `edge-tts --list-voices | findstr <iso>` after the package is installed.

## Prerequisites

- Windows (Stop hook uses Windows MCI for MP3 playback; voice-in uses Windows-specific window automation via pywinauto. Linux/Mac support is TODO)
- Python 3.9+
- Internet access (edge-tts uses Microsoft's online TTS endpoint; Whisper model + espeak-ng are local once installed)
- For voice-in only: ~1.5 GB of binary deps — whisper.cpp + a ggml Whisper model + espeak-ng — provisioned once via `--gpu auto` (auto-detects the card) or manually per the README "Voice input — binary dependencies".
