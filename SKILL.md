---
name: claude-speech
description: Scaffold a language tutor in any project — Claude speaks target-language phrases aloud while English notes stay silent. Use when the user asks to learn or practice a foreign language with spoken feedback, or when they want Claude's non-English responses read aloud in Claude Code.
---

# claude-speech

This skill bootstraps a self-contained language-learning project inside the user's current Claude Code workspace:
- a teacher persona (`CLAUDE.md`) for the chosen language,
- a `Stop` hook + `scripts/speak_lang.py` that uses `edge-tts` to speak only the target-language portion of Claude's replies aloud,
- a `UserPromptSubmit` hook + `scripts/push_to_talk.py` + `scripts/inject_transcript.py` for **F9 push-to-talk voice input** that transcribes via local Whisper, converts to IPA via espeak-ng, and pastes the transcription into the chat as your message.

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
     2. Delete `<project_root>/recordings/latest_transcript.txt` if it exists, so the UserPromptSubmit hook doesn't keep re-injecting the last transcript on subsequent manual Enters now that the daemon isn't writing fresh ones.
     3. Report the PIDs killed (or "no daemons were running") and confirm the stale transcript was cleared.
   - Any other argument is treated as a language name or ISO code — proceed with steps 1+ below.

1. **Ask the user which language** to teach. Accept names ("Dutch", "German", "Polish") or ISO 639-1 codes ("nl", "de", "pl"). Default list of supported languages lives in `voices.json` next to this skill — open it if the user asks what's available.

2. **Ask if they want a non-default voice.** Each language has a recommended `edge-tts` voice; if the user wants something different (different gender, accent, or specific neural voice), they can pass it as `--voice <voice-id>`.

3. **Resolve the target directory.** Use `$CLAUDE_PROJECT_DIR` (the current Claude Code project root). If that env var is missing, fall back to the current working directory. Confirm the target with the user before writing files.

4. **Run the installer** (from this skill's directory):
   ```
   py install.py --lang <name> [--voice <voice-id>] [--target <dir>] [--force] [--no-voice-in]
   ```
   This writes `CLAUDE.md`, `.claude/settings.json`, `scripts/speak_lang.py`, `scripts/push_to_talk.py`, and `scripts/inject_transcript.py` into the target. It also pip-installs `edge-tts` and the voice-in deps (`numpy sounddevice scipy pynput pywinauto pyperclip`) if missing. Pass `--no-voice-in` to skip the push-to-talk pieces (TTS-only setup).

5. **Confirm next steps** with the user: open the target dir in a fresh Claude Code session (or reload `/config` if already inside), then say hi — the assistant will greet them in the chosen language with the agreed tag convention.

6. **Auto-start the push-to-talk daemon (if it exists in the target).** If `<target>/scripts/push_to_talk.py` is present (i.e. the user didn't pass `--no-voice-in`), spawn it in the background with the chosen language code:
   - Before spawning, kill any prior instance to avoid duplicate keyboard listeners (use the same PowerShell command from step 0).
   - Spawn detached so it survives this turn. From Claude Code's Bash tool, use `run_in_background=true` with: `py "<target>\\scripts\\push_to_talk.py" --lang <code>`
   - Confirm to the user that the daemon is running, which hotkey to hold (default F9), and remind them they may need to set `--window-title-re` if the auto-submit picks the wrong window. Tell them to run with `--list-windows` once to see candidates.
   - **Important:** the daemon will not be functional until the user has manually installed the binary dependencies (`whisper-cli.exe`, the ggml model, and `espeak-ng.exe`) — see README's "Voice input — binary dependencies" section. The Python scaffold is ready; the binaries are BYO.

## Tag convention

Generated `CLAUDE.md` instructs the assistant to wrap every target-language utterance in `<{code}>...</{code}>` tags (where `{code}` is the ISO 639-1 code: `<nl>`, `<de>`, `<es>`, etc.). The Stop hook extracts only that content and sends it to TTS. Anything outside the tags — English pedagogical notes, corrections, follow-up questions — stays silent and only appears as text.

## Adding a new language

Edit `voices.json`. Add an object with `name`, `code`, `iso`, `voice` fields. To find an `edge-tts` voice id, run `edge-tts --list-voices | findstr <iso>` after the package is installed.

## Prerequisites

- Windows (Stop hook uses Windows MCI for MP3 playback; voice-in uses Windows-specific window automation via pywinauto. Linux/Mac support is TODO)
- Python 3.9+
- Internet access (edge-tts uses Microsoft's online TTS endpoint; Whisper model + espeak-ng are local once installed)
- For voice-in only: ~1.5 GB of binary deps the user provisions manually — whisper.cpp + a ggml Whisper model + espeak-ng. See README "Voice input — binary dependencies".
