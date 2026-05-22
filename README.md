# claude-speech

A Claude Code skill that turns any project into a **language-learning workspace** with selective text-to-speech: Claude speaks target-language phrases aloud, English pedagogical notes stay silent.

Unlike whole-response TTS plugins, `claude-speech` reads **only** the text inside language tags, so mixed-language replies (Dutch sentence + English correction) sound natural — you hear the part you're practicing, you read the part that explains it.

## How it works

The skill scaffolds three files into your project:

| File | Role |
|---|---|
| `CLAUDE.md` | Teacher persona for the chosen language, with tag rules |
| `.claude/settings.json` | Stop hook wired to the TTS script |
| `scripts/speak_lang.py` | Extracts tagged text, synthesizes via `edge-tts`, plays via Windows MCI |

When Claude finishes a reply, the Stop hook reads the transcript, pulls every `<{code}>...</{code}>` block (where `{code}` is the ISO 639-1 code of the language you're learning), and pipes them to `edge-tts` for playback.

## The language tag convention

Each language has a **2-letter tag** matching ISO 639-1:

| Language | Tag | Default voice |
|---|---|---|
| Dutch | `<nl>` | nl-NL-FennaNeural |
| German | `<de>` | de-DE-KatjaNeural |
| Spanish | `<es>` | es-ES-ElviraNeural |
| French | `<fr>` | fr-FR-DeniseNeural |
| Italian | `<it>` | it-IT-ElsaNeural |
| Portuguese | `<pt>` | pt-PT-RaquelNeural |
| Russian | `<ru>` | ru-RU-SvetlanaNeural |
| Polish | `<pl>` | pl-PL-ZofiaNeural |
| Japanese | `<ja>` | ja-JP-NanamiNeural |
| Chinese | `<zh>` | zh-CN-XiaoxiaoNeural |
| English | `<en>` | en-US-JennyNeural |

The generated `CLAUDE.md` tells the assistant:

> Wrap every utterance in {language} inside `<{code}>...</{code}>` tags.
> English pedagogical notes go outside the tags and stay silent.

### Worked example (learning Dutch)

A typical reply looks like:

```
<nl>Goedemorgen! Hoe gaat het vandaag met je?</nl>

(Started with a basic greeting — try answering with "Het gaat goed".)
```

The script extracts only the Dutch sentence and plays it. The English note appears on screen but is never spoken — so you can read it at your own pace while practicing the spoken part.

You can also have multiple Dutch utterances in one reply:

```
<nl>Dat is goed!</nl> <nl>Wil je nu over eten praten?</nl>

(Two short prompts — the second one offers a topic to continue.)
```

Both blocks are concatenated and played back-to-back.

## Installation

### As a personal skill

```powershell
git clone https://github.com/Dimoniada/claude-speech "$env:USERPROFILE\.claude\skills\claude-speech"
```

The skill auto-discovers from `~/.claude/skills/`.

### Manual one-off setup (no skill install)

You can also just clone the repo anywhere and run `install.py` directly:

```powershell
git clone https://github.com/Dimoniada/claude-speech D:\Tools\claude-speech
py D:\Tools\claude-speech\install.py --lang Dutch --target D:\Data\my-dutch-project
```

## Usage

### From within Claude Code

Invoke the skill and tell Claude:
> "Set up a Dutch tutor here."

Claude will resolve the target directory from `$CLAUDE_PROJECT_DIR` (Claude Code sets this automatically per session), confirm with you, and run the installer.

### Manually

```powershell
# Default voice
py install.py --lang Dutch

# Override voice
py install.py --lang German --voice de-DE-ConradNeural

# Explicit target dir (otherwise $CLAUDE_PROJECT_DIR, otherwise CWD)
py install.py --lang Spanish --target D:\Data\spanish-practice

# Overwrite existing files
py install.py --lang Dutch --force
```

After install, open the target directory in Claude Code and start chatting. The first time you say "hi", the assistant greets you in the chosen language and your speakers play the audio.

## Adding a new language

Edit `voices.json`. Find a voice id from the full edge-tts catalogue:

```powershell
edge-tts --list-voices | findstr nl-
```

Add an entry:

```json
{"name": "Norwegian", "code": "no", "iso": "nb-NO", "voice": "nb-NO-PernilleNeural"}
```

Re-run the installer with `--lang Norwegian`.

## Troubleshooting

**No audio plays.**
Check `<target>/logs/speak_lang.log`. Common causes: edge-tts couldn't reach the Microsoft endpoint (firewall), or the assistant's reply has no `<{code}>...</{code}>` tags (the skill stays silent in that case by design).

**Audio plays in English.**
Make sure the assistant actually wrapped its Dutch (or chosen-language) text in the right tags. Open the transcript file referenced by the hook payload and look for `<nl>...</nl>` blocks.

**Wrong voice / want a different speaker.**
Edit `.claude/settings.json` in your target project and change the `--voice` argument. List voices with `edge-tts --list-voices`.

**Hook fires in every Claude Code session even outside a language project.**
The Stop hook is scoped to the *project* `.claude/settings.json` — it does not affect other projects unless you also installed it there.

## Prerequisites

- **Windows** — playback uses Windows MCI via `ctypes`. Linux/Mac support is a TODO (would swap `play_mp3` for `simpleaudio` or `playsound`).
- **Python 3.9+** — `py` launcher should be on PATH.
- **Internet access** — edge-tts uses Microsoft's online TTS endpoint.

## Why not just use an existing TTS plugin?

[claude-speak](https://github.com/silverdolphin863/claude-speak) and [claude-voice-system](https://github.com/Secondvisitation783/claude-voice-system) are great if you want the whole reply spoken. For language learning that doesn't work — you want the foreign sentence read aloud, but the English explanation kept silent (otherwise you can't have mixed-language pedagogical replies). `claude-speech` solves exactly that gap by reading only what's tagged.

## License

MIT — see [LICENSE](LICENSE).
