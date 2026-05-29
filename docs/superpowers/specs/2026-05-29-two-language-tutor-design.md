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
2. Voice input (F9) auto-detects which of the two languages the learner spoke
   and handles each appropriately.
3. The teacher persona writes all meta-text in the common language.

## Non-goals

- Speaking the common language aloud (it is intentionally silent).
- Generating IPA for the common language.
- Supporting more than two languages simultaneously.
- Changing the TTS / Stop-hook path beyond what the persona change requires.

## Decisions (from brainstorming)

| Decision | Choice |
|----------|--------|
| Recognition strategy | Whisper `-l auto` per utterance (single pass on the happy path) |
| IPA scope | Target language only |
| Notes language | Common language (Russian, etc.) |
| Common language source | 2nd positional arg to the skill; ask if missing |
| Misdetection (detected ∉ {target, common}) | Re-run forcing the **common** language; treat as common (no IPA) |
| Flag name | `--common` (and `--target`, with `--lang` kept as an alias) |

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

- Whisper invoked with `-l auto` instead of a forced language.
- Parse the detected-language code from whisper's stderr
  (`auto-detected language: XX (p = ...)`).
- Resolution rule:

  | Detected | Resolved as | Payload |
  |----------|-------------|---------|
  | target   | target      | `text` + `\n[IPA]` (target espeak voice) |
  | common   | common      | `text` |
  | neither  | common (re-run forcing `-l <common>`) | `text` |

- IPA is generated **only** when the resolved language is the target.
- Recording filename uses the resolved code: `rec_<code>_NNNN.wav`.
- CLI flags:
  - `--target <code>` (new canonical name; `--lang` kept as a hidden/back-compat alias mapping to target).
  - `--common <code>` (new, required for two-language behavior).
  - Existing `--espeak-*`, `--hotkey`, `--window-title-re`, `--no-auto-submit`,
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
F9 held → record 16kHz mono WAV
       → whisper-cli -l auto  → (text, detected_code)
       → resolve(detected_code, target, common):
            target → IPA via espeak(target voice); payload = text + "\n[" + ipa + "]"
            common → payload = text
            neither → re-run whisper -l <common>; payload = text
       → write latest_transcript.txt
       → auto-submit to Claude Code window (fallback: UserPromptSubmit hook)
```

## Error handling

- Whisper stderr lacks a detectable language line → treat as common (safe
  default: no IPA, plain text).
- Forced-common re-run fails → fall back to the original auto-pass text.
- espeak-ng failure on a target utterance → payload falls back to text only
  (existing behavior).

## Testing

- Unit: detected-language parser against sample whisper stderr (target, common,
  third-language, and missing-line cases).
- Unit: resolution rule table (4 rows) → correct payload shape and IPA gating.
- Manual: speak target → text + IPA; speak common → text only; speak a third
  language → snaps to common, plain text.
- Installer: `--target nl --common ru` renders CLAUDE.md with Russian notes and
  a Dutch tag convention; unknown common errors out.

## Backward compatibility

- `--lang` remains accepted on `push_to_talk.py` as an alias for `--target`.
- Single-language installs without `--common`: the skill prompts for the common
  language; the installer requires it. (No silent single-language mode — the new
  model always has two languages.)

## Out of scope / follow-ups

- Pre-existing bug: the Mandarin Chinese entry in `voices.json` has a Russian
  `voice` (`ru-RU-SvetlanaNeural`). Fix separately.
- Phase 2+ binary-dep auto-download and GPU build pipeline (tracked elsewhere).
