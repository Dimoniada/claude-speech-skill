---
name: claude-speech
description: Scaffold a language tutor in any project — Claude speaks target-language phrases aloud while English notes stay silent. Use when the user asks to learn or practice a foreign language with spoken feedback, or when they want Claude's non-English responses read aloud in Claude Code.
---

# claude-speech

This skill bootstraps a self-contained language-learning project inside the user's current Claude Code workspace: a teacher persona (CLAUDE.md), a Stop hook that captures Claude's reply, and a Python script that uses `edge-tts` to speak only the target-language portion aloud.

## When to use

Trigger when the user says any of:
- "let's practice {language}"
- "teach me {language}"
- "set up a {language} tutor"
- "I want Claude to speak {language} responses"
- "scaffold claude-speech for {language}"

## How to invoke

1. **Ask the user which language** to teach. Accept names ("Dutch", "German", "Polish") or ISO 639-1 codes ("nl", "de", "pl"). Default list of supported languages lives in `voices.json` next to this skill — open it if the user asks what's available.

2. **Ask if they want a non-default voice.** Each language has a recommended `edge-tts` voice; if the user wants something different (different gender, accent, or specific neural voice), they can pass it as `--voice <voice-id>`.

3. **Resolve the target directory.** Use `$CLAUDE_PROJECT_DIR` (the current Claude Code project root). If that env var is missing, fall back to the current working directory. Confirm the target with the user before writing files.

4. **Run the installer** (from this skill's directory):
   ```
   py install.py --lang <name> [--voice <voice-id>] [--target <dir>] [--force]
   ```
   This writes `CLAUDE.md`, `.claude/settings.json`, and `scripts/speak_lang.py` into the target. It also pip-installs `edge-tts` for the user if missing.

5. **Confirm next steps** with the user: open the target dir in a fresh Claude Code session (or reload `/config` if already inside), then say hi — the assistant will greet them in the chosen language with the agreed tag convention.

## Tag convention

Generated `CLAUDE.md` instructs the assistant to wrap every target-language utterance in `<{code}>...</{code}>` tags (where `{code}` is the ISO 639-1 code: `<nl>`, `<de>`, `<es>`, etc.). The Stop hook extracts only that content and sends it to TTS. Anything outside the tags — English pedagogical notes, corrections, follow-up questions — stays silent and only appears as text.

## Adding a new language

Edit `voices.json`. Add an object with `name`, `code`, `iso`, `voice` fields. To find an `edge-tts` voice id, run `edge-tts --list-voices | findstr <iso>` after the package is installed.

## Prerequisites

- Windows (Stop hook script uses Windows MCI for MP3 playback; Linux/Mac support is TODO)
- Python 3.9+
- Internet access (edge-tts uses Microsoft's online TTS endpoint)
