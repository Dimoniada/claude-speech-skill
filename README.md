# claude-speech

A Claude Code skill that turns any project into a **language-learning workspace** with two-way voice:

- **Output (TTS)** — Claude speaks target-language phrases aloud, native-language notes stay silent.
- **Input (push-to-talk, two keys)** — hold **F9** to speak the language you're learning, or **F10** to speak your native language. Release, and local Whisper transcribes in the language the held key forces (no auto-detection, so mixed-language speech isn't misread). For the learned language espeak-ng adds an IPA line; your message then appears in the chat as your message automatically.

Unlike whole-response TTS plugins, `claude-speech` reads **only** the text inside language tags, so mixed-language replies (a learned-language sentence + a native-language correction) sound natural — you hear the part you're practicing, you read the part that explains it. The voice-input side keeps everything local: no audio leaves your machine.

## How it works

The skill scaffolds these files into your project:

| File | Role |
|---|---|
| `CLAUDE.md` | Teacher persona for the chosen language, with tag rules |
| `.claude/settings.json` | `Stop` hook (TTS) + `UserPromptSubmit` hook (voice-in fallback) |
| `scripts/speak_lang.py` | Extracts tagged text from Claude's reply, synthesizes via `edge-tts`, plays via Windows MCI |
| `scripts/push_to_talk.py` | Two-key daemon (F9 = learned language + IPA, F10 = native language): records mic → Whisper → IPA → pastes into chat |
| `scripts/inject_transcript.py` | UserPromptSubmit hook that injects the last transcript if auto-paste couldn't focus the chat window |

**Output flow.** When Claude finishes a reply, the Stop hook reads the transcript, pulls every `<{code}>...</{code}>` block (where `{code}` is the ISO 639-1 code of the language you're learning), and pipes them to `edge-tts` for playback.

**Input flow.** The push-to-talk daemon runs in a separate terminal. Hold **F9** (learned language) or **F10** (native language) anywhere — both are global hotkeys → it records 16 kHz mono PCM → release the key → it sends the clip to a local resident `whisper-server` (started once and kept warm in VRAM), forcing the language bound to the key you held (no auto-detection) → for the learned language it then runs `espeak-ng --ipa` to render IPA → finally it focuses your Claude Code window and pastes the payload (`text` alone for the native language, `text\n[IPA]` for the learned one) + Enter. If window-focus is blocked by Windows 11 anti-focus-stealing, it falls back to writing `recordings/latest_transcript.txt`, and the `UserPromptSubmit` hook injects it on your next manual Enter.

> **Focusing the input.** The daemon focuses the chat input box for you. The Claude app is Electron and exposes its whole web view as a single accessibility node, so there's no distinct text control to target by type — instead the daemon locates the input as the focusable container in the bottom strip of the window (a UIA `Group` spanning most of the width) and calls `set_focus` on it, which lands the caret in the message box without touching your mouse. This works even if the caret was on a button or the sidebar beforehand. **If a transcript ever fails to appear in the input, the auto-focus couldn't find the box** (unusual window layout, a Claude UI change, or more than one matching container) — click the input box once yourself and try again; with the caret already there the paste lands regardless.

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
> Notes and corrections (written in your native language) go outside the tags and stay silent.

### Worked example (learning Dutch)

A typical reply looks like:

```
<nl>Goedemorgen! Hoe gaat het vandaag met je?</nl>

(Started with a basic greeting — try answering with "Het gaat goed".)
```

The script extracts only the Dutch sentence and plays it. The note appears on screen but is never spoken — so you can read it at your own pace while practicing the spoken part. (The notes here are shown in English for the README; in a real session they're written in whichever native language you passed as `--common`.)

You can also have multiple Dutch utterances in one reply:

```
<nl>Dat is goed!</nl> <nl>Wil je nu over eten praten?</nl>

(Two short prompts — the second one offers a topic to continue.)
```

Both blocks are concatenated and played back-to-back.

## Voice input (push-to-talk)

After running `install.py` *and* installing the binary deps (see next section), open a separate terminal and start the daemon:

```powershell
py D:\Data\my-dutch-project\scripts\push_to_talk.py --target nl --common ru
```

The daemon prints a banner showing which window will receive the auto-paste:

```
============================================================
Push-to-talk active.
  Hold F9 to speak TARGET 'nl' (transcribed as nl + IPA via espeak-ng voice 'nl').
  Hold F10 to speak COMMON 'ru' (transcribed as ru, no IPA).
The key you hold forces the language — no auto-detection. Ctrl+C to quit.
Auto-submit target (matches for '.*Claude.*'):
  0. 'Claude'  <- will use
NOTE: the daemon focuses the chat input automatically (UIA).
      If a transcript ever fails to appear, click the input box
      once and try again — the auto-focus couldn't locate it.
============================================================
```

Hold **F9**, speak in the language you're learning, release. After ~1 s on a modest CPU (faster with the optional Vulkan build — see below) your message appears in the chat as two lines: the orthographic text Whisper recognized, plus the IPA in brackets so you can audit your pronunciation. (Hold **F10** instead to speak your native language — it arrives as plain text, no IPA.) For example, after saying "Ik ga naar de winkel" you'll see:

```
Ik ga naar de winkel
[ɪk ɣˈaː naːr də ʋˈɪŋkəl]
```

### Useful flags

| Flag | Purpose |
|---|---|
| `--target <code>` | The language you're learning — spoken aloud + IPA. Mirrors `voices.json` codes. Required. |
| `--common <code>` | Your native/communication language — notes only, never spoken, no IPA. Required. |
| `--target-hotkey <key>` | Override the key held to speak the target language. Default `f9`. |
| `--common-hotkey <key>` | Override the key held to speak the common language. Default `f10`. |
| `--input-device <name\|index>` | Microphone to record from — a device index or a substring of its name. Default: system default. Prefer a name (indices aren't stable across reboots). |
| `--list-devices` | Print available audio input/output devices (index + name + host API) and exit. |
| `--list-windows` | Print all visible top-level window titles and exit. Use to discover the right `--window-title-re`. |
| `--window-title-re '<regex>'` | Regex matching the Claude Code window to paste into. Default `.*Claude.*`. If multiple windows match, the first is picked — disambiguate with a more specific regex like `^Claude$` or `^Claude-Tutor$`. |
| `--no-auto-submit` | Skip the auto-paste + Enter. Daemon only writes `recordings/latest_transcript.txt`; the `UserPromptSubmit` hook injects on your manual Enter. Use this if auto-paste keeps targeting the wrong window. |
| `--no-enter` | Paste the transcript into the chat input but don't press Enter, so you can review/edit it before sending. |
| `--espeak-voice <voice>` | Override the espeak-ng voice for IPA. Default derived from `--target` (e.g. `en` → `en-us`, `zh` → `cmn`). |
| `--model <path>` | Override the ggml whisper model path. |

### Choosing audio devices

By default the daemon records from the system default microphone and TTS plays on the system default output. You can pin both to specific devices — useful when, say, the default keeps flipping to a Bluetooth headset's low-quality hands-free mic.

First, list what's available:

```powershell
py scripts\push_to_talk.py --list-devices
```

This prints every input and output device with its index, name, and host API (MME / DirectSound / WASAPI / …). The same hardware usually appears several times, once per host API — that's normal.

**Always prefer a name substring over an index.** Device indices are reassigned across reboots and when you plug/unplug devices, so an index baked into a config can silently point at the wrong thing later. A name substring (`"USB PnP"`, `"OnePlus"`) is matched at runtime and survives reordering. When a name matches several host-API entries for the same hardware, the lowest index is used (and the alternatives are logged).

- **Microphone (input):** pass `--input-device` to the daemon:
  ```powershell
  py scripts\push_to_talk.py --target nl --common ru --input-device "USB PnP"
  ```
- **Speaker/headphone (output):** pass `--output-device` to the **installer**, which bakes it into the Stop hook in `.claude/settings.json`:
  ```powershell
  py install.py --target Dutch --common Russian --output-device "Headphones"
  ```
  When an output device is chosen, TTS no longer plays through the dependency-free Windows MCI path; instead the edge-tts MP3 is decoded with [`miniaudio`](https://pypi.org/project/miniaudio/) and played via `sounddevice` on that endpoint. The installer pip-installs `miniaudio` automatically in that case. With no `--output-device`, nothing changes — playback stays on MCI and the system default.

You can also test output routing directly:

```powershell
py scripts\speak_lang.py --list-devices
```

### Turning voice off and back on

In a Claude Code session, the skill responds to a control argument:

```
/claude-speech off
```

This is a full off switch for **both** directions of voice:

- **Voice in** — terminates every running `push_to_talk.py` daemon and clears any pending `latest_transcript.txt` so the fallback hook doesn't keep re-injecting stale content.
- **Voice out** — runs `toggle_voice.py --off`, which surgically removes the `speak_lang.py` Stop hook from `.claude/settings.json` (your other hooks and settings are left untouched) and stashes an exact copy in `.claude/speak_lang.hook.json`. Spoken replies stop firing even if Claude still emits `<{code}>` tags. (The hook fires every turn once installed — there's no runtime "skill is active" state in Claude Code — so removing it is the only honest way to silence output.)

Aliases: `stop`, `kill`.

**To re-enable, just invoke `/claude-speech` again with no language argument.** When a stash exists, the skill restores the Stop hook from it and restarts the daemon — no re-interview, same language/voice/device as before.

Invoking it *with* a language (`/claude-speech German Russian`) instead runs a full re-initialization for that language and turns voice back on. The skill recovers your previous settings (language, voice, devices) and offers them as defaults, so you only answer what's changing. You never need to type `--force` — it's an internal installer flag the skill manages on your behalf when an existing setup has to be regenerated.

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

**Automatic option.** Instead of the manual blocks below, run
`py install.py … --gpu auto` (or `py provision_whisper.py --project-dir <dir> --gpu auto`).
It detects your GPU and provisions the matching backend — NVIDIA gets the prebuilt CUDA zip,
AMD/Intel compile Vulkan from source, otherwise CPU — plus the ggml model and espeak-ng.
Use `--detect-only` first to see the plan (sizes, time, what's already installed) without
downloading anything. Already-installed dependencies are skipped; any failure stops and rolls
back the in-project artifacts it created (system SDKs are left in place). The manual steps below
remain valid and are what `--gpu` automates.

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

# Result: $proj\tools\whisper.cpp\bin\Release\whisper-server.exe (plus DLLs)
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
    │   ├── bin\Release\whisper-server.exe  (+ ggml-*.dll, whisper.dll, …)
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
# Result: $proj\tools\whisper.cpp\bin\Release\whisper-server.exe with CUDA support
```

**Step 3 — install cuDNN (Option B only).** The CUDA 12.4 zip already bundles cuDNN, so skip this step if you used Option A. For Option B (CUDA 11.8), you need cuDNN's runtime libraries. The simplest way is to pip-install the bundled wheels into the same Python the daemon uses:

```powershell
py -m pip install --user nvidia-cublas-cu11 nvidia-cudnn-cu11
```

Alternatively, download cuDNN 8.x for CUDA 11 from [developer.nvidia.com/cudnn](https://developer.nvidia.com/cudnn-downloads) and copy `cudnn*.dll` next to `whisper-server.exe`.

**Step 4 — verify.** Start the server once and watch its startup banner:

```powershell
& "$proj\tools\whisper.cpp\bin\Release\whisper-server.exe" `
    -m "$proj\tools\whisper.cpp\models\ggml-medium-q5_0.bin" `
    --host 127.0.0.1 --port 8910
```

As it loads the model it should print a line like:

```
ggml_cuda_init: found 1 CUDA devices:
  Device 0: NVIDIA GeForce RTX 4070, compute capability 8.9, VMM: yes
```

followed by `whisper server listening at http://127.0.0.1:8910`. That's how you know CUDA is active; press **Ctrl+C** to stop it (the push-to-talk daemon starts and stops this server for you automatically — this manual run is only to confirm the GPU backend). If you see no `ggml_cuda_init` line (or you see `ggml_vulkan: …` from a leftover Vulkan build), the swap didn't take — re-check the zip extracted to the right path.

If `whisper-server.exe` fails to start with `Could not load library cudnn_ops_infer64_*.dll` or similar, cuDNN isn't on the DLL search path. Re-do Step 3, or fall back to Option A which bundles it.

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

CMake's configure output should include `-- Found Vulkan` and `-- Including Vulkan backend`. The first run of `whisper-server.exe` after the swap will print `ggml_vulkan: Found N Vulkan devices: ...` — that's how you know GPU is active.

This path is automated by `py install.py … --gpu vulkan` (or `--gpu auto` on an AMD/Intel box) — see "Automatic option" above. The manual steps here are the fallback and the reference for what the automation does.

## Installation

### As a personal skill

```powershell
git clone https://github.com/Dimoniada/claude-speech-skill "$env:USERPROFILE\.claude\skills\claude-speech"
```

The skill auto-discovers from `~/.claude/skills/`.

### Manual one-off setup (no skill install)

You can also just clone the repo anywhere and run `install.py` directly:

```powershell
git clone https://github.com/Dimoniada/claude-speech-skill D:\Tools\claude-speech
py D:\Tools\claude-speech\install.py --target Dutch --common Russian --project-dir D:\Data\my-dutch-project
```

## Usage

### From within Claude Code

Invoke the skill and tell Claude:
> "Set up a Dutch tutor here."

Claude will resolve the project directory from `$CLAUDE_PROJECT_DIR` (Claude Code sets this automatically per session), confirm with you, and run the installer.

### Manually

`--target` is the target **language**; the scaffold destination is `--project-dir` (`--lang` is still accepted as a hidden alias for `--target`).

```powershell
# Default voice
py install.py --target Dutch --common Russian

# Override voice
py install.py --target German --common Russian --voice de-DE-ConradNeural

# Explicit project dir (otherwise $CLAUDE_PROJECT_DIR, otherwise CWD)
py install.py --target Spanish --common Russian --project-dir D:\Data\spanish-practice

# Overwrite existing files
py install.py --target Dutch --common Russian --force

# TTS-only — skip the push-to-talk scripts and their Python deps
py install.py --target Dutch --common Russian --no-voice-in
```

After install, open the project directory in Claude Code and start chatting. The first time you say "hi", the assistant greets you in the chosen language and your speakers play the audio.

If you want push-to-talk too, follow the "Voice input — binary dependencies" section above to provision the three binary deps, then run `py …\scripts\push_to_talk.py --target <code> --common <code>` in a separate terminal.

## Adding a new language

Edit `voices.json`. Find a voice id from the full edge-tts catalogue:

```powershell
edge-tts --list-voices | findstr nl-
```

Add an entry:

```json
{"name": "Norwegian", "code": "no", "iso": "nb-NO", "voice": "nb-NO-PernilleNeural"}
```

Re-run the installer with `--target Norwegian --common <your-language>`.

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

**Spoken words don't appear in the input box on their own.**
Normally the daemon focuses the chat input for you (it finds the input container via UIA and calls `set_focus`, no mouse needed) — check `logs/push_to_talk.log` for `focused chat input via UIA Group`. If that line is missing, the auto-focus couldn't identify the input box unambiguously (an unusual window size, a Claude UI change, or more than one matching container). In that case **move the keyboard focus into the input yourself**: click the message box once so the caret blinks there, then hold the key and speak — the paste lands wherever the caret is.

**Paste still doesn't work even with the caret in the box.**
The daemon pastes with a low-level Win32 `Ctrl+V` (`keybd_event`) rather than pywinauto's `send_keys("^v")`, because the Electron-based Claude app silently ignores the synthetic Ctrl+V from `send_keys`. If you adapted the script and paste stopped working, make sure you kept the `keybd_event` paste path (or switch it to Shift+Insert, which the app also accepts).

**Auto-submit pastes into the wrong window.**
The daemon picks the first window matching `--window-title-re` (default `.*Claude.*`). If you have multiple Claude-titled windows (e.g. the Claude Desktop client plus your Claude Code terminal), the first match might not be your chat. Run once with `--list-windows` to see all candidates, then restart with a more specific regex:

```powershell
py scripts\push_to_talk.py --target nl --common ru --window-title-re "^Claude-Tutor$"
```

**`ERROR: missing dependency '<name>' for this Python interpreter.`**
You ran the daemon with a different `py` interpreter than the one `install.py` installed deps into. Run the suggested `py -m pip install --user …` line from the error message — it uses the same interpreter as the failing daemon launch.

**`whisper-server.exe not found` / `espeak-ng.exe not found`.**
The Python scaffold is set up but the binary deps aren't. Follow the "Voice input — binary dependencies" section above.

**Whisper transcribes the wrong language.**
The language is forced by which key you hold — **F9** decodes as your target language, **F10** as your common language. If a clip comes out in the wrong language, you almost certainly held the wrong key. Also confirm `--target` / `--common` were set to the codes you intended when you started the daemon.

**Daemon's terminal shows IPA garbled as `???` or hex.**
PowerShell defaults to cp1252 codepage. The daemon already forces UTF-8 on stdout via `sys.stdout.reconfigure(encoding='utf-8')`, but if you're piping the daemon's output through another tool that re-encodes, you may need `chcp 65001` in your terminal session.

## Prerequisites

- **Windows** — TTS playback uses Windows MCI via `ctypes`; voice-input window automation uses `pywinauto`. Linux/Mac support is a TODO.
- **Python 3.9+** — `py` launcher should be on PATH.
- **Internet access** — edge-tts uses Microsoft's online TTS endpoint. Voice-input is fully local once binary deps are installed.
- **For voice-input only** — ~1.5 GB of binary deps you provision manually (whisper.cpp, ~540 MB ggml model, ~80 MB espeak-ng). See "Voice input — binary dependencies".

## Why not just use an existing TTS plugin?

[claude-speak](https://github.com/silverdolphin863/claude-speak) and [claude-voice-system](https://github.com/Secondvisitation783/claude-voice-system) are great if you want the whole reply spoken. For language learning that doesn't work — you want the foreign sentence read aloud, but the native-language explanation kept silent (otherwise you can't have mixed-language pedagogical replies). `claude-speech` solves exactly that gap by reading only what's tagged.

## License

MIT — see [LICENSE](LICENSE).
