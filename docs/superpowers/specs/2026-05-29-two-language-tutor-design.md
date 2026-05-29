# Two-language tutor: target + common language

**Date:** 2026-05-29
**Status:** Approved (design)

## Problem

The `claude-speech` skill currently scaffolds a tutor for a single language: the
target language is spoken aloud (TTS), and pedagogical notes are hardcoded to
English. A learner whose native language is not English has no first-class way
to (a) read notes/corrections in their own language, or (b) speak to the tutor
in their native language and have the voice-input pipeline transcribe it
correctly.

This change introduces a **two-language model**:

- **Target language** — the language being learned. Spoken aloud via edge-tts,
  tagged for the Stop hook, and the only language that receives an IPA
  pronunciation aid on voice input.
- **Common language** — the learner's native/communication language. Used for
  all notes, corrections, and free conversation. Never spoken aloud. Never gets
  an IPA line.

## Goals

1. Skill invocation takes two languages: `/claude-speech <target> <common>`.
2. Voice input uses two keys — F9 (target) and F10 (common) — and the held key
   forces the transcription language; no auto-detection.
3. The teacher persona writes all meta-text in the common language.

## Non-goals

- Speaking the common language aloud (it is intentionally silent).
- Generating IPA for the common language.
- Supporting more than two languages simultaneously.
- Changing the TTS / Stop-hook path beyond what the persona change requires.

## Decisions (from brainstorming)

| Decision | Choice |
|----------|--------|
| Recognition strategy | **Two push-to-talk keys** decide the language — no auto-detection. The held key forces whisper's `-l <code>`. |
| Target key | F9 (configurable via `--target-hotkey`) → forces target, adds IPA |
| Common key | F10 (configurable via `--common-hotkey`) → forces common, no IPA |
| IPA scope | Target language only |
| Notes language | Common language (Russian, etc.) |
| Common language source | 2nd positional arg to the skill; ask if missing |
| Flag name | `--common` (and `--target`, with `--lang` kept as an alias) |

**Why two keys instead of auto-detect:** whisper auto-detection runs per clip
and fails on mixed-language utterances — e.g. the Dutch words "zijn en hebben"
spoken inside a mostly-Russian sentence were transcribed as the English
"sign and habit". Letting the key choose the language removes the guess.

## Components and changes

### 1. SKILL.md

- `/claude-speech <target> <common>`:
  - Arg 1 = target language (name or ISO code).
  - Arg 2 = common language (name or ISO code).
  - If arg 2 is missing, ask for it (mirror the existing arg-1 prompt).
- `off` / `stop` / `kill` control path unchanged.
- Step 4 (installer call) passes `--common`.
- Step 6 (daemon spawn) passes `--target` and `--common` to `push_to_talk.py`.

### 2. push_to_talk.py

- Two hotkeys are registered. `record_until_release(hotkey_map)` listens for
  either and returns `(audio, lang)` where `lang` is the code bound to the key
  that was held.
- Whisper is invoked forcing that language (`-l <lang>`); no detection step.
- IPA is generated **only** when the held key is the target key.
- Recording filename uses the forced code: `rec_<code>_NNNN.wav`.
- CLI flags:
  - `--target <code>` (canonical; `--lang` kept as a hidden/back-compat alias).
  - `--common <code>` (required for two-language behavior).
  - `--target-hotkey <key>` (default `f9`), `--common-hotkey <key>`
    (default `f10`). `--hotkey` kept as a hidden alias for `--target-hotkey`.
  - Existing `--espeak-*`, `--window-title-re`, `--no-auto-submit`,
    `--list-windows` unchanged.
- The espeak voice used for IPA continues to derive from the target code
  (`LANG_TO_ESPEAK_VOICE`).

### 3. CLAUDE.md template

- Spoken target sentences stay wrapped in `<{{LANG_CODE}}>...</{{LANG_CODE}}>`.
- All notes/corrections/meta written in the common language.
- New placeholders: `{{COMMON_NAME}}`, `{{COMMON_CODE}}`.
- Persona behavior:
  - Learner speaks/types the common language → treat as native chat, keep teaching.
  - Learner attempts the target → correct them, with the correction written in
    the common language.
- The "meta question" rule (reply with no tags so TTS stays silent) is reworded
  to say the silent reply is in the common language.

### 4. install.py

- New `--common <name|code>` argument, resolved via the existing
  `find_language()` against `voices.json`.
- Render `{{COMMON_NAME}}` / `{{COMMON_CODE}}` into CLAUDE.md.
- `voices.json` already contains a Russian entry — no addition required. The
  `voice` field of the common language is unused (common is never spoken).
- Validation: error with the available-language list if either target or common
  is unknown, same as today's single-language path.

### 5. settings.json template

- No structural change required. The Stop hook still extracts the target tag;
  the UserPromptSubmit hook is unchanged.

## Data flow (voice input)

```
F9 or F10 held → record 16kHz mono WAV  (lang = code bound to the held key)
       → whisper-cli -l <lang>  → text
       → if lang == target: payload = text + "\n[" + IPA(target voice) + "]"
         else:               payload = text
       → write latest_transcript.txt
       → auto-submit to Claude Code window (fallback: UserPromptSubmit hook)
```

## Error handling

- whisper-cli returns empty text → skip submit, log and continue.
- espeak-ng failure on a target utterance → payload falls back to text only.
- Target and common hotkeys resolve to the same key → startup error.

## Testing

- Unit: `parse_whisper_json` against sample whisper JSON (text + missing-field
  and garbage cases). Language selection is by keypress, so there is no
  detection rule to unit-test.
- Manual: hold F9 + speak target → text + IPA; hold F10 + speak common →
  text only; mixed-language sentence under the correct key → transcribed in the
  intended language (no English mis-mapping).
- Installer: `--target nl --common ru` renders CLAUDE.md with Russian notes and
  a Dutch tag convention; unknown common errors out.

## Backward compatibility

- `--lang` remains accepted on `push_to_talk.py` as an alias for `--target`.
- Single-language installs without `--common`: the skill prompts for the common
  language; the installer requires it. (No silent single-language mode — the new
  model always has two languages.)

## Out of scope / follow-ups

- Phase 2+ binary-dep auto-download and GPU build pipeline (tracked elsewhere).
