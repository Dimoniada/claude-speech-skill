# claude-speech

A Claude Code skill that turns any project into a **language-learning workspace** with two-way voice:

- **Output (TTS)** — Claude speaks target-language phrases aloud, English pedagogical notes stay silent.
- **Input (F9 push-to-talk)** — hold F9, speak in your target language, release. Local Whisper transcribes, espeak-ng converts to IPA, and your spoken sentence (with phonetic transcription) appears in the chat as your message automatically.

Unlike whole-response TTS plugins, `claude-speech` reads **only** the text inside language tags, so mixed-language replies (Dutch sentence + English correction) sound natural — you hear the part you're practicing, you read the part that explains it. The voice-input side keeps everything local: no audio leaves your machine.

## How it works

The skill scaffolds these files into your project:

| File | Role |
|---|---|
| `CLAUDE.md` | Teacher persona for the chosen language, with tag rules |
| `.claude/settings.json` | `Stop` hook (TTS) + `UserPromptSubmit` hook (voice-in fallback) |
| `scripts/speak_lang.py` | Extracts tagged text from Claude's reply, synthesizes via `edge-tts`, plays via Windows MCI |
| `scripts/push_to_talk.py` | F9-driven daemon: records mic → Whisper → IPA → pastes into chat |
| `scripts/inject_transcript.py` | UserPromptSubmit hook that injects the last transcript if auto-paste couldn't focus the chat window |

**Output flow.** When Claude finishes a reply, the Stop hook reads the transcript, pulls every `<{code}>...</{code}>` block (where `{code}` is the ISO 639-1 code of the language you're learning), and pipes them to `edge-tts` for playback.

**Input flow.** The push-to-talk daemon runs in a separate terminal. Hold F9 anywhere (global hotkey, focus-agnostic) → it records 16 kHz mono PCM → release F9 → it calls a local `whisper-cli.exe` to transcribe → then `espeak-ng --ipa` to render IPA → finally focuses your Claude Code window and pastes the two-line `text\n[IPA]` payload + Enter. If window-focus is blocked by Windows 11 anti-focus-stealing, it falls back to writing `recordings/latest_transcript.txt`, and the `UserPromptSubmit` hook injects it on your next manual Enter.

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

## Voice input (F9 push-to-talk)

After running `install.py` *and* installing the binary deps (see next section), open a separate terminal and start the daemon:

```powershell
py D:\Data\my-dutch-project\scripts\push_to_talk.py --lang nl
```

The daemon prints a banner showing which window will receive the auto-paste:

```
============================================================
Push-to-talk active for language 'nl' (IPA via espeak-ng voice 'nl').
Hold F9 anywhere to record. Ctrl+C to quit.
Auto-submit target (matches for '.*Claude.*'):
  0. 'Claude'  <- will use
============================================================
Hold F9 to record, release to transcribe.
```

Hold **F9**, speak in your target language, release. After ~1 s on a modest CPU (faster with the optional Vulkan build — see below) your message appears in the chat as two lines: the orthographic text Whisper recognized, plus the IPA in brackets so you can audit your pronunciation. For example, after saying "Ik ga naar de winkel" you'll see:

```
Ik ga naar de winkel
[ɪk ɣˈaː naːr də ʋˈɪŋkəl]
```

### Useful flags

| Flag | Purpose |
|---|---|
| `--lang <code>` | Target language. Mirrors `voices.json` codes. Default `en`. |
| `--hotkey <key>` | Override push-to-talk key. Default `f9`. Examples: `f10`, `f12`. |
| `--list-windows` | Print all visible top-level window titles and exit. Use to discover the right `--window-title-re`. |
| `--window-title-re '<regex>'` | Regex matching the Claude Code window to paste into. Default `.*Claude.*`. If multiple windows match, the first is picked — disambiguate with a more specific regex like `^Claude$` or `^Claude-Tutor$`. |
| `--no-auto-submit` | Skip the auto-paste + Enter. Daemon only writes `recordings/latest_transcript.txt`; the `UserPromptSubmit` hook injects on your manual Enter. Use this if auto-paste keeps targeting the wrong window. |
| `--espeak-voice <voice>` | Override the espeak-ng voice for IPA. Default derived from `--lang` (e.g. `en` → `en-us`, `zh` → `cmn`). |
| `--model <path>` | Override the ggml whisper model path. |

### Disabling / stopping the daemon

In a Claude Code session, the skill responds to a control argument:

```
/claude-speech off
```

This finds every running `push_to_talk.py` daemon, terminates it, and clears any pending `latest_transcript.txt` so the fallback hook doesn't keep re-injecting stale content. Aliases: `stop`, `kill`.

To re-enable, run `py …\scripts\push_to_talk.py --lang …` in a fresh terminal, or just invoke `/claude-speech <language>` again — the skill auto-spawns the daemon as step 6 of its setup.

## Voice input — binary dependencies

The Python scaffold (`push_to_talk.py`, `inject_transcript.py`) is shipped by `install.py`. The **binary deps are not** — too large for a git repo, and license/distribution rules differ. You provision them yourself, once, into the project's `tools/` directory.

Assuming your project is `D:\Data\my-project`, run the commands below in PowerShell from that directory. The three blocks are independent and can be done in any order.

### Picking a whisper.cpp backend

The CPU/BLAS path below is the **default and recommended starting point** — it works on every Windows machine with no extra system software, downloads in seconds, and gives ~2 s end-to-end latency on a modern CPU. Two GPU upgrade paths exist if you want sub-second latency; you'd swap them in after confirming the CPU path works.

| Backend | Hardware | Latency for 5 s of audio | Setup effort | Notes |
|---|---|---|---|---|
| **CPU + OpenBLAS** (default) | any x64 CPU | ~1.5–2 s warm | trivial | Pre-built zip from upstream. No drivers, no toolkit. **Start here.** |
| **CUDA** | NVIDIA GPU | ~0.3–0.8 s warm | low | Pre-built zip from upstream. Requires CUDA Toolkit installed on the box (or use the bundled-cuDNN zip for zero extra installs). |
| **Vulkan** | any GPU (AMD, Intel, NVIDIA) | ~0.5–1 s warm | high | No pre-built upstream binary — you compile from source. Requires VS Build Tools + Vulkan SDK. Documented at the bottom of this section. |

For most users the right order is: get CPU working first, then upgrade only if the latency annoys you in real use.

### 1. whisper.cpp (CPU build) — ~16 MB

```powershell
$proj = $PWD.Path  # e.g. D:\Data\my-project
mkdir "$proj\tools\whisper.cpp\bin" -Force | Out-Null
mkdir "$proj\tools\whisper.cpp\models" -Force | Out-Null

# Download upstream CPU+BLAS release
$zip = "$proj\tools\whisper.cpp\whisper-blas.zip"
Invoke-WebRequest -Uri "https://github.com/ggerganov/whisper.cpp/releases/download/v1.8.4/whisper-blas-bin-x64.zip" -OutFile $zip
Expand-Archive -Path $zip -DestinationPath "$proj\tools\whisper.cpp\bin" -Force
Remove-Item $zip

# Result: $proj\tools\whisper.cpp\bin\Release\whisper-cli.exe (plus DLLs)
```

### 2. Whisper model — ~540 MB (multilingual, quantized medium)

```powershell
Invoke-WebRequest `
  -Uri "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin" `
  -OutFile "$proj\tools\whisper.cpp\models\ggml-medium-q5_0.bin"
```

For smaller installs use `ggml-small-q5_1.bin` (~180 MB, English-only-friendly) or `ggml-base.bin` (~150 MB, multilingual but lower accuracy). Pass the chosen path via `--model` when starting the daemon.

### 3. espeak-ng (for IPA conversion) — ~80 MB

The MSI installer normally writes into `C:\Program Files\eSpeak NG\` and the Windows Registry. To keep everything in-project, do an MSI **administrative install** which just extracts the payload:

```powershell
$msi = "$proj\tools\espeak-ng.msi"
Invoke-WebRequest -Uri "https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi" -OutFile $msi

# Admin-extract (no Program Files install, no registry entries)
Start-Process msiexec.exe `
  -ArgumentList '/a',('"' + $msi + '"'),'/qn',('TARGETDIR="' + $proj + '\tools\espeak-extract"') `
  -Wait -NoNewWindow

# Flatten the nested "eSpeak NG" subdir into tools\espeak-ng\
Move-Item "$proj\tools\espeak-extract\eSpeak NG" "$proj\tools\espeak-ng"
Remove-Item "$proj\tools\espeak-extract" -Recurse -Force
Remove-Item $msi

# Sanity check — should print IPA for "transcription"
$env:ESPEAK_DATA_PATH = "$proj\tools\espeak-ng\espeak-ng-data"
& "$proj\tools\espeak-ng\espeak-ng.exe" -v en-us --ipa -q "transcription"
```

The daemon sets `ESPEAK_DATA_PATH` automatically when it shells out to `espeak-ng.exe`; you don't need to keep that env var in your shell.

### Final layout

```
your-project\
├── .claude\settings.json
├── CLAUDE.md
├── scripts\
│   ├── speak_lang.py
│   ├── push_to_talk.py
│   └── inject_transcript.py
├── recordings\               (created on first F9 release)
├── logs\
└── tools\
    ├── whisper.cpp\
    │   ├── bin\Release\whisper-cli.exe  (+ ggml-*.dll, whisper.dll, …)
    │   └── models\ggml-medium-q5_0.bin
    └── espeak-ng\
        ├── espeak-ng.exe
        ├── libespeak-ng.dll
        └── espeak-ng-data\
```

### Optional: CUDA build (NVIDIA GPUs) — easiest GPU path

If you have an NVIDIA GPU, the upstream whisper.cpp releases ship pre-built CUDA binaries. **No compile needed** — it's the same drop-in pattern as the CPU build, just a different zip. End-to-end latency drops from ~2 s to ~0.3–0.8 s on a midrange NVIDIA card.

**Step 1 — confirm your CUDA Toolkit version.** Open PowerShell and run:

```powershell
nvidia-smi
```

The top-right corner shows a line like `CUDA Version: 12.4`. That number is the **maximum CUDA version your driver supports**, not necessarily what's installed. If it says `12.x`, use the CUDA 12.4 zip below; if it's older (or you specifically have CUDA 11.x installed), use the CUDA 11.8 zip.

If `nvidia-smi` isn't found, you either don't have an NVIDIA GPU or the driver isn't installed. In that case use the CPU path (or the Vulkan path further down for AMD/Intel).

**Step 2 — download and extract.** Pick **one** of these two zips:

```powershell
$proj = $PWD.Path  # e.g. D:\Data\my-project
mkdir "$proj\tools\whisper.cpp\bin" -Force | Out-Null
mkdir "$proj\tools\whisper.cpp\models" -Force | Out-Null

# Wipe the CPU build if you installed it first
Remove-Item "$proj\tools\whisper.cpp\bin\Release" -Recurse -Force -ErrorAction SilentlyContinue

$zip = "$proj\tools\whisper.cpp\whisper-cublas.zip"

# Option A — CUDA 12.4, ~457 MB (BUNDLES cuDNN — nothing else to install)
Invoke-WebRequest -Uri "https://github.com/ggerganov/whisper.cpp/releases/download/v1.8.4/whisper-cublas-12.4.0-bin-x64.zip" -OutFile $zip

# Option B — CUDA 11.8, ~58 MB (you must install cuDNN separately, see Step 3)
# Invoke-WebRequest -Uri "https://github.com/ggerganov/whisper.cpp/releases/download/v1.8.4/whisper-cublas-11.8.0-bin-x64.zip" -OutFile $zip

Expand-Archive -Path $zip -DestinationPath "$proj\tools\whisper.cpp\bin" -Force
Remove-Item $zip
# Result: $proj\tools\whisper.cpp\bin\Release\whisper-cli.exe with CUDA support
```

**Step 3 — install cuDNN (Option B only).** The CUDA 12.4 zip already bundles cuDNN, so skip this step if you used Option A. For Option B (CUDA 11.8), you need cuDNN's runtime libraries. The simplest way is to pip-install the bundled wheels into the same Python the daemon uses:

```powershell
py -m pip install --user nvidia-cublas-cu11 nvidia-cudnn-cu11
```

Alternatively, download cuDNN 8.x for CUDA 11 from [developer.nvidia.com/cudnn](https://developer.nvidia.com/cudnn-downloads) and copy `cudnn*.dll` next to `whisper-cli.exe`.

**Step 4 — verify.** Run the binary against any 16 kHz WAV:

```powershell
& "$proj\tools\whisper.cpp\bin\Release\whisper-cli.exe" `
    -m "$proj\tools\whisper.cpp\models\ggml-medium-q5_0.bin" `
    -f any_16khz.wav -l en -nt -np
```

On the first run it should print a line like:

```
ggml_cuda_init: found 1 CUDA devices:
  Device 0: NVIDIA GeForce RTX 4070, compute capability 8.9, VMM: yes
```

That's how you know CUDA is active. If you see no such line (or you see `ggml_vulkan: …` from a leftover Vulkan build), the swap didn't take — re-check the zip extracted to the right path.

If `whisper-cli.exe` fails to start with `Could not load library cudnn_ops_infer64_*.dll` or similar, cuDNN isn't on the DLL search path. Re-do Step 3, or fall back to Option A which bundles it.

### Optional: Vulkan build (any GPU) — for AMD/Intel, or NVIDIA users who prefer it

If you're on AMD or Intel (where the CUDA path doesn't apply) — or you're on NVIDIA but prefer Vulkan — you can build whisper.cpp from source with the Vulkan backend. Inference drops from ~2 s to ~0.5–1 s per utterance on a modern discrete GPU. Unlike CUDA, there's **no pre-built upstream binary** for Vulkan, so you compile from source.

Prerequisites — these are system-wide installs:

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools --silent --accept-source-agreements --accept-package-agreements --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows11SDK.22621 --add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended"
winget install --id KhronosGroup.VulkanSDK --silent --accept-source-agreements --accept-package-agreements
```

Build:

```powershell
$proj = $PWD.Path
git clone --depth 1 --branch v1.8.4 https://github.com/ggerganov/whisper.cpp "$proj\tools\whisper.cpp-src"

$env:VULKAN_SDK = (Get-ChildItem "C:\VulkanSDK" | Select-Object -Last 1).FullName
$cmake = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"

cd "$proj\tools\whisper.cpp-src"
& $cmake -B build -DGGML_VULKAN=ON
& $cmake --build build --config Release -j

# Replace the CPU binary with the Vulkan one
Remove-Item "$proj\tools\whisper.cpp\bin\Release" -Recurse -Force
Copy-Item "$proj\tools\whisper.cpp-src\build\bin\Release" "$proj\tools\whisper.cpp\bin\Release" -Recurse
```

CMake's configure output should include `-- Found Vulkan` and `-- Including Vulkan backend`. The first run of `whisper-cli.exe` after the swap will print `ggml_vulkan: Found N Vulkan devices: ...` — that's how you know GPU is active.

This entire path will eventually be automated as `install.py --gpu vulkan` (Phase 3 in the roadmap) and then as a pre-built CI binary download (Phase 4). Today it's manual.

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

# TTS-only — skip the push-to-talk scripts and their Python deps
py install.py --lang Dutch --no-voice-in
```

After install, open the target directory in Claude Code and start chatting. The first time you say "hi", the assistant greets you in the chosen language and your speakers play the audio.

If you want F9 push-to-talk too, follow the "Voice input — binary dependencies" section above to provision the three binary deps, then run `py …\scripts\push_to_talk.py --lang <code>` in a separate terminal.

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

**No audio plays AND `logs/speak_lang.log` doesn't exist at all.**
The hook process itself isn't starting — Python is erroring out before it can write a log line. Almost always this means the `command` path in `.claude/settings.json` is wrong (e.g. the project was renamed or copied from a different scaffold). Open `.claude/settings.json` and confirm the path resolves. Recent installs use `$CLAUDE_PROJECT_DIR` so they survive renames; older installs may have an absolute path baked in.

**Audio plays in English.**
Make sure the assistant actually wrapped its Dutch (or chosen-language) text in the right tags. Open the transcript file referenced by the hook payload and look for `<nl>...</nl>` blocks.

**Wrong voice / want a different speaker.**
Edit `.claude/settings.json` in your target project and change the `--voice` argument. List voices with `edge-tts --list-voices`.

**Hook fires in every Claude Code session even outside a language project.**
The Stop hook is scoped to the *project* `.claude/settings.json` — it does not affect other projects unless you also installed it there.

### Voice input

**F9 records but nothing appears in the chat (silent failure).**
The daemon found the right window and called `set_focus()`, but Windows 11's anti-focus-stealing protection blocked it — so the paste went to whatever was focused. The daemon ships with an "Alt-tap" workaround that fixes this in most cases, plus a foreground-handle verification that aborts if focus didn't actually move. Check `logs/push_to_talk.log` for a line like `foreground window is not target (target=..., fg=...) — aborting`. If you see that, the auto-submit path can't help — use the fallback by manually pressing Enter in the Claude window (the `UserPromptSubmit` hook will inject the transcript), or run with `--no-auto-submit`.

**Auto-submit pastes into the wrong window.**
The daemon picks the first window matching `--window-title-re` (default `.*Claude.*`). If you have multiple Claude-titled windows (e.g. the Claude Desktop client plus your Claude Code terminal), the first match might not be your chat. Run once with `--list-windows` to see all candidates, then restart with a more specific regex:

```powershell
py scripts\push_to_talk.py --lang en --window-title-re "^Claude-Tutor$"
```

**`ERROR: missing dependency '<name>' for this Python interpreter.`**
You ran the daemon with a different `py` interpreter than the one `install.py` installed deps into. Run the suggested `py -m pip install --user …` line from the error message — it uses the same interpreter as the failing daemon launch.

**`whisper-cli.exe not found` / `espeak-ng.exe not found`.**
The Python scaffold is set up but the binary deps aren't. Follow the "Voice input — binary dependencies" section above.

**Whisper transcribes the wrong language.**
Pass `--lang <code>` matching what you're actually speaking. The default is `en`. The model is multilingual; the flag just tells Whisper which language to decode.

**Daemon's terminal shows IPA garbled as `???` or hex.**
PowerShell defaults to cp1252 codepage. The daemon already forces UTF-8 on stdout via `sys.stdout.reconfigure(encoding='utf-8')`, but if you're piping the daemon's output through another tool that re-encodes, you may need `chcp 65001` in your terminal session.

## Prerequisites

- **Windows** — TTS playback uses Windows MCI via `ctypes`; voice-input window automation uses `pywinauto`. Linux/Mac support is a TODO.
- **Python 3.9+** — `py` launcher should be on PATH.
- **Internet access** — edge-tts uses Microsoft's online TTS endpoint. Voice-input is fully local once binary deps are installed.
- **For voice-input only** — ~1.5 GB of binary deps you provision manually (whisper.cpp, ~540 MB ggml model, ~80 MB espeak-ng). See "Voice input — binary dependencies".

## Why not just use an existing TTS plugin?

[claude-speak](https://github.com/silverdolphin863/claude-speak) and [claude-voice-system](https://github.com/Secondvisitation783/claude-voice-system) are great if you want the whole reply spoken. For language learning that doesn't work — you want the foreign sentence read aloud, but the English explanation kept silent (otherwise you can't have mixed-language pedagogical replies). `claude-speech` solves exactly that gap by reading only what's tagged.

## License

MIT — see [LICENSE](LICENSE).
