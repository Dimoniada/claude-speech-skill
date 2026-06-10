"""Selection toolbar: select text anywhere, get a floating 🔊 button, hear it aloud.

Runs as its own background process alongside push_to_talk.py. A global mouse
listener watches for a left-button drag-and-release (a text selection); on
release it copies the selection to the clipboard, then shows a small borderless,
always-on-top toolbar at the cursor. Clicking 🔊 reads the captured text aloud
using the same edge-tts path as the Stop hook (scripts/speak_lang.py), with the
voice and output device taken from .claude/claude_speech.json.

This is task 1 of two: the toolbar framework + the Read button. A 🌐 Translate
button (with a translation popup) is planned next and slots in beside 🔊.

Scope: by default the toolbar appears only inside the Claude app (window titles
starting with "Claude"). Pass --window-title-re '<regex>' to override the scope
(e.g. '^Claude' for Claude-only, or '' for any application). Note it always
speaks with the configured (target-language) voice, so non-target text elsewhere
will be mispronounced.

Usage (from a separate terminal, leave it running):
    py selection_toolbar.py
    py selection_toolbar.py --window-title-re "^Claude"
    py selection_toolbar.py --voice nl-NL-FennaNeural --output-device "Headphones"

Press Ctrl+C in this terminal to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Force UTF-8 on stdout/stderr so printing selected text (which may contain
# non-ASCII) doesn't crash in PowerShell's default cp1252.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# Third-party / GUI deps — print an actionable hint instead of a raw traceback.
try:
    import tkinter as tk
    import pyperclip
    from pynput import mouse
except ImportError as _exc:
    missing = _exc.name or "a required package"
    print(
        f"ERROR: missing dependency '{missing}' for this Python interpreter.\n"
        f"       Interpreter : {sys.executable}\n"
        f"       Install with: py -m pip install --user pynput pyperclip\n"
        f"       (tkinter ships with the standard Python installer — if it's\n"
        f"        missing, reinstall Python with the 'tcl/tk' option enabled.)",
        file=sys.stderr,
    )
    sys.exit(2)

SELF_PATH = Path(__file__).resolve()
SCRIPT_DIR = SELF_PATH.parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / ".claude" / "claude_speech.json"
LOG_PATH = PROJECT_ROOT / "logs" / "selection_toolbar.log"

# speak_lang.py is a sibling in scripts/. Its heavy deps (edge_tts, sounddevice,
# miniaudio) are imported lazily inside its functions, so importing the module
# is cheap and side-effect-free — we reuse it rather than duplicate TTS code.
sys.path.insert(0, str(SCRIPT_DIR))
import speak_lang  # noqa: E402
from cs_common import load_project_config  # noqa: E402

DEFAULT_DRAG_THRESHOLD = 6   # px of pointer travel that counts as a selection drag
DEFAULT_TIMEOUT_MS = 6000    # auto-dismiss the toolbar after this long (when idle)
CLIPBOARD_SETTLE_S = 0.06    # let the OS populate the clipboard after Ctrl+C
TOOLBAR_OFFSET_X = 12        # place the toolbar to the lower-right of the
TOOLBAR_OFFSET_Y = 16        # selection end (mouse-release point)
COLOR_IDLE = "white"         # button foreground when inactive
COLOR_BUSY = "#6b6c6e"       # button foreground while its action is active (grayed)
POPUP_MARGIN = 2             # uniform inset for the translation popup's contents
DEFAULT_TOOLBAR_WINDOW_RE = r"^Claude"  # default scope: only inside the Claude app
# Anchored at the start so a browser page that merely mentions "Claude" mid-title
# (e.g. "Anthropic's Claude - Chrome") doesn't count — only the app, whose window
# title starts with "Claude", does. Match is case-sensitive (re.search).


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


# --- pure logic (unit-tested) ----------------------------------------------

def is_drag(press_xy: tuple[int, int], release_xy: tuple[int, int], threshold: int) -> bool:
    """True when the pointer moved at least `threshold` px between press and
    release — i.e. a drag (a likely text selection) rather than a click."""
    dx = abs(release_xy[0] - press_xy[0])
    dy = abs(release_xy[1] - press_xy[1])
    return (dx * dx + dy * dy) >= threshold * threshold


def selection_anchor(press_xy: tuple[int, int], release_xy: tuple[int, int]) -> tuple[int, int]:
    """The bottom-right corner of the drag's bounding box — where the toolbar
    anchors. Using max() of the press and release points makes it the lower-right
    of the selected text regardless of drag direction (left↔right, up↔down)."""
    return (max(press_xy[0], release_xy[0]), max(press_xy[1], release_xy[1]))


def window_allowed(foreground_title: str, window_title_re: str | None) -> bool:
    """Scope gate: with no regex the toolbar works in any app; with a regex it
    only fires when the foreground window title matches."""
    if not window_title_re:
        return True
    return re.search(window_title_re, foreground_title or "") is not None


def resolve_window_re(cli_value: str | None, config: dict) -> str | None:
    """Effective scope filter: an explicit --window-title-re wins; otherwise the
    project config's `toolbar_window_re` (which may be null = everywhere); and if
    that key is absent entirely, default to Claude-only."""
    if cli_value is not None:
        return cli_value or None
    if "toolbar_window_re" in (config or {}):
        return config["toolbar_window_re"]
    return DEFAULT_TOOLBAR_WINDOW_RE


def clamp_to_screen(x: int, y: int, w: int, h: int, screen_w: int, screen_h: int,
                    margin: int = 4) -> tuple[int, int]:
    """Nudge a w×h popup placed at (x, y) so it stays fully on screen."""
    x = max(margin, min(x, screen_w - w - margin))
    y = max(margin, min(y, screen_h - h - margin))
    return x, y


def capture_selection(copy_fn, get_clip, set_clip, prev_clip, settle: float = 0.0) -> str:
    """Copy the current selection and return it, leaving the clipboard as it was.

    `copy_fn` triggers a copy (Ctrl+C); `get_clip`/`set_clip` read/write the
    clipboard. The previous clipboard contents (`prev_clip`) are restored so the
    user's clipboard is undisturbed. Injectable for tests (no real keyboard)."""
    copy_fn()
    if settle:
        time.sleep(settle)
    captured = get_clip()
    try:
        set_clip(prev_clip if prev_clip is not None else "")
    except Exception:
        pass
    return captured or ""


# --- Win32 helpers ----------------------------------------------------------

def send_ctrl_c() -> None:
    """Low-level Win32 Ctrl+C (keybd_event), mirroring push_to_talk's Ctrl+V.
    Synthetic Ctrl+C via this API registers as a real copy in Electron/Chromium
    apps where pywinauto's send_keys('^c') is silently ignored."""
    VK_CONTROL = 0x11
    VK_C = 0x43
    KEYEVENTF_KEYUP = 0x0002
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_C, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def foreground_window_title() -> str:
    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(handle)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buf, length + 1)
    return buf.value


def set_dpi_awareness() -> None:
    """Make the process DPI-aware so tkinter places windows in physical pixels,
    matching the coordinates pynput reports for the mouse. Without this, on a
    scaled display (125%/150%) the toolbar lands progressively further from the
    cursor the closer you are to the bottom-right of the screen."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --- TTS (reuses speak_lang.py) --------------------------------------------

def speak_text(text: str, voice: str, output_device_spec: str | None,
               rate: str, _speak_lang=speak_lang) -> bool:
    """Synthesize `text` with edge-tts and play it, reusing speak_lang's helpers.
    Returns True on success. `_speak_lang` is injectable for tests."""
    if not text or not voice:
        return False
    try:
        device = _speak_lang.resolve_output_device(output_device_spec)
    except ValueError as exc:
        logging.error("output device: %s", exc)
        device = None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = Path(f.name)
        asyncio.run(_speak_lang.synthesize(text, voice, rate, tmp))
        _speak_lang.play_mp3(tmp, device)
        return True
    except Exception as exc:
        logging.exception("speak failed: %s", exc)
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def compute_ipa(text: str, target_code: str | None) -> str:
    """IPA transcription of `text`, reusing push_to_talk.to_ipa (espeak-ng) and
    its bundled binary paths — the same code path F9 uses. Returns "" if the
    daemon module or espeak-ng isn't available, so the popup still shows the
    translation."""
    if not text or not target_code:
        return ""
    try:
        import push_to_talk as ptt
        voice = ptt.LANG_TO_ESPEAK_VOICE.get(target_code, target_code)
        return ptt.to_ipa(text, voice, ptt.DEFAULT_ESPEAK_NG, ptt.DEFAULT_ESPEAK_DATA)
    except Exception as exc:
        logging.warning("IPA generation unavailable: %s", exc)
        return ""


def ensure_argos_package(from_code: str, to_code: str) -> bool:
    """Make sure the offline argostranslate model for from_code->to_code is
    installed, downloading it once if needed (the only step that needs internet).
    Returns True when a translation path is available."""
    try:
        import argostranslate.package as package
        import argostranslate.translate as translate
    except Exception as exc:
        logging.error("argostranslate not installed: %s", exc)
        return False
    installed = {lang.code for lang in translate.get_installed_languages()}
    if from_code in installed and to_code in installed:
        return True
    try:
        package.update_package_index()
        available = package.get_available_packages()
        match = next((p for p in available if p.from_code == from_code and p.to_code == to_code), None)
        if match is None:
            logging.error("no argostranslate package for %s->%s", from_code, to_code)
            return False
        logging.info("downloading argostranslate model %s->%s (one-time)", from_code, to_code)
        package.install_from_path(match.download())
        return True
    except Exception as exc:
        logging.exception("argostranslate package install failed: %s", exc)
        return False


def translate_text(text: str, from_code: str, to_code: str, translate_fn=None) -> str:
    """Translate `text` from `from_code` to `to_code` with argostranslate (offline).
    `translate_fn(text, from, to)` is injectable for tests; defaults to the
    argostranslate API. Returns "" on failure."""
    text = (text or "").strip()
    if not text or not from_code or not to_code:
        return ""
    try:
        if translate_fn is None:
            import argostranslate.translate as at
            translate_fn = at.translate
        return (translate_fn(text, from_code, to_code) or "").strip()
    except Exception as exc:
        logging.exception("translate failed: %s", exc)
        return ""


# --- toolbar UI -------------------------------------------------------------

class SelectionToolbar:
    """Owns the tkinter root and the floating toolbar. The mouse listener runs
    on another thread and calls request_show(), which marshals onto the tk main
    thread via root.after() — tkinter is not thread-safe to touch directly."""

    def __init__(self, voice: str, output_device: str | None, rate: str,
                 target_code: str | None = None, common_code: str | None = None,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.voice = voice
        self.output_device = output_device
        self.rate = rate
        self.target_code = target_code
        self.common_code = common_code
        self.timeout_ms = timeout_ms
        self.root = tk.Tk()
        self.root.withdraw()  # never show the root; only the Toplevel toolbar
        self._toolbar: tk.Toplevel | None = None
        self._timer: str | None = None
        self._captured = ""
        self._speaking = False
        self._speak_proc: subprocess.Popen | None = None
        self._popup: tk.Toplevel | None = None
        self._trans_frame: tk.Frame | None = None
        self._btn_h = 22
        self._trans_text: tk.Text | None = None
        self._ipa_text: tk.Text | None = None
        self._ipa_frame: tk.Frame | None = None
        self._ipa_btn: tk.Button | None = None
        self._ipa_visible = False
        self._read_btn: tk.Button | None = None
        self._translate_btn: tk.Button | None = None
        self._tb_x = self._tb_y = self._tb_h = 0

    def is_showing(self) -> bool:
        """True while a toolbar is on screen. The mouse listener checks this to
        avoid re-triggering (and stealing focus / sending Ctrl+C) when the user
        starts a *different* drag — e.g. selecting a screenshot region in
        Flameshot — while our toolbar is still up."""
        return self._toolbar is not None

    # called from the mouse-listener thread
    def request_show(self, x: int, y: int, text: str) -> None:
        self.root.after(0, lambda: self._show(x, y, text))

    def _show(self, x: int, y: int, text: str) -> None:
        self._captured = text
        self._destroy_toolbar()

        tb = tk.Toplevel(self.root)
        tb.overrideredirect(True)              # no title bar / border
        tb.wm_attributes("-topmost", True)     # float above the app
        try:
            tb.wm_attributes("-alpha", 0.97)
        except tk.TclError:
            pass

        frame = tk.Frame(tb, bg="#202123", bd=1, relief="solid")
        frame.pack()
        # Translate first, then Read, then close (left-to-right via side="left").
        translate_btn = tk.Button(
            frame, text="🌐", font=("Segoe UI Emoji", 12), bg="#202123", fg=COLOR_IDLE,
            activebackground="#3a3b3d", bd=0, padx=10, pady=4, cursor="hand2",
            command=self._on_translate,
        )
        translate_btn.pack(side="left")
        self._translate_btn = translate_btn
        read_btn = tk.Button(
            frame, text="🔊", font=("Segoe UI Emoji", 12), bg="#202123", fg=COLOR_IDLE,
            activebackground="#3a3b3d", bd=0, padx=10, pady=4, cursor="hand2",
            command=self._on_read,
        )
        read_btn.pack(side="left")
        self._read_btn = read_btn
        tk.Button(
            frame, text="✕", font=("Segoe UI", 9), bg="#202123", fg="#9a9b9e",
            activebackground="#3a3b3d", bd=0, padx=8, pady=4, cursor="hand2",
            command=self._close,
        ).pack(side="left")

        # Size the window, then place it to the lower-right of the selection's
        # bottom-right corner — clear of the text and the pointer — clamped on-screen.
        tb.update_idletasks()
        w, h = tb.winfo_width(), tb.winfo_height()
        px, py = clamp_to_screen(
            x + TOOLBAR_OFFSET_X, y + TOOLBAR_OFFSET_Y, w, h,
            tb.winfo_screenwidth(), tb.winfo_screenheight(),
        )
        tb.geometry(f"+{px}+{py}")
        tb.bind("<Escape>", lambda _e: self._close())

        self._toolbar = tb
        self._tb_x, self._tb_y, self._tb_h = px, py, h
        self._refresh_timer()

    def _on_read(self) -> None:
        if self._toolbar is None:
            return
        # Toggle: a second click while speaking interrupts (terminates the child).
        if self._speaking:
            self._stop_playback()
            return
        text = self._captured
        if not text:
            return
        # Speak in a SEPARATE PROCESS, not a thread in this UI process: edge-tts
        # playback (PortAudio) can abort hard when interrupted, and we never want
        # that to take down the toolbar. Stopping is just terminate().
        try:
            cmd = [sys.executable, str(SELF_PATH), "--speak", "--voice", self.voice, "--rate", self.rate]
            if self.output_device is not None:
                cmd += ["--output-device", str(self.output_device)]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            logging.exception("failed to start speak subprocess: %s", exc)
            return
        self._speaking = True
        self._speak_proc = proc
        self._set_read_active(True)   # gray while speaking
        self._refresh_timer()         # keep the toolbar up for the whole utterance

        def feed_and_wait() -> None:
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()
            try:
                self.root.after(0, self._on_read_done)
            except Exception:
                pass

        threading.Thread(target=feed_and_wait, daemon=True).start()

    def _on_read_done(self) -> None:
        # Runs on the tk thread after the speak child exits (finished or killed).
        self._speaking = False
        self._speak_proc = None
        if self._toolbar is None:
            return  # closed while speaking
        self._set_read_active(False)  # back to white
        self._refresh_timer()

    def _on_translate(self) -> None:
        if self._toolbar is None:
            return
        # Toggle: a second click hides the translation and un-grays the button.
        if self._popup is not None:
            self._destroy_popup()
            self._set_translate_active(False)
            self._refresh_timer()
            return
        text = self._captured
        if not text:
            return
        # Keep everything up while translating and while the user reads the
        # result — don't auto-dismiss until they toggle it off or close.
        self._set_translate_active(True)
        self._show_result_popup()
        self._refresh_timer()

        def worker() -> None:
            ipa = compute_ipa(text, self.target_code)
            translation = ""
            if self.target_code and self.common_code and ensure_argos_package(self.target_code, self.common_code):
                translation = translate_text(text, self.target_code, self.common_code)
            try:
                self.root.after(0, lambda: self._fill_result_popup(text, ipa, translation))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_result_popup(self) -> None:
        self._destroy_popup()
        if self._toolbar is None:
            return
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)            # completely borderless, no chrome
        pop.wm_attributes("-topmost", True)
        pop.geometry(f"440x200+{self._tb_x}+{self._tb_y + self._tb_h + 4}")
        pop.bind("<Escape>", lambda _e: self._close_popup())

        inner = tk.Frame(pop, bg="#202123")
        inner.pack(fill="both", expand=True)

        # Translation fills the ENTIRE window (placed, not packed), so the only
        # solid element is the floating IPA button — everywhere else shows text.
        trans_frame = tk.Frame(inner, bg="#202123")
        trans_frame.place(x=POPUP_MARGIN, y=POPUP_MARGIN,
                          relwidth=1.0, width=-2 * POPUP_MARGIN,
                          relheight=1.0, height=-2 * POPUP_MARGIN)
        trans = tk.Text(trans_frame, wrap="word", bg="#202123", fg="white", bd=0,
                        font=("Segoe UI", 11), padx=6, pady=4)
        trans.pack(side="left", fill="both", expand=True)
        self._attach_scroll_indicator(trans_frame, trans)  # slim position indicator

        # IPA panel (hidden until expanded): occupies the bottom half when shown.
        ipa_frame = tk.Frame(inner, bg="#1a1b1c")
        ipa = tk.Text(ipa_frame, wrap="word", bg="#1a1b1c", fg="#9fd0ff", bd=0, height=4,
                      font=("Segoe UI", 10), padx=6, pady=4)
        ipa.pack(side="left", fill="both", expand=True)
        self._attach_scroll_indicator(ipa_frame, ipa)  # same slim indicator

        # The only solid chrome: a small IPA toggle floating at the bottom-left.
        ipa_btn = tk.Button(inner, text="▸ IPA", anchor="w", bg="#2b2c2e", fg="#cfd0d2",
                            activebackground="#3a3b3d", bd=0, padx=8, pady=0, cursor="hand2",
                            font=("Segoe UI", 8), command=self._toggle_ipa)
        ipa_btn.place(relx=0.0, x=POPUP_MARGIN, rely=1.0, y=-POPUP_MARGIN, anchor="sw")

        # Borderless => no visible grip; resize by dragging the invisible right /
        # bottom edges and corner (cursor changes are the only hint).
        self._install_resize_edges(pop, inner)

        self._set_text(trans, "Translating…")
        self._popup = pop
        self._trans_frame = trans_frame
        self._trans_text = trans
        self._ipa_text = ipa
        self._ipa_frame = ipa_frame
        self._ipa_btn = ipa_btn
        self._ipa_visible = False
        try:
            ipa_btn.update_idletasks()
            self._btn_h = ipa_btn.winfo_height() or 22
        except Exception:
            self._btn_h = 22
        self._layout_translation()

    def _fill_result_popup(self, text: str, ipa: str, translation: str) -> None:
        # The origin text is intentionally NOT shown — only the translation, with
        # its IPA transcription available under the collapsible toggle.
        if self._popup is None:
            return
        self._set_text(self._trans_text, translation or "(translation unavailable — see logs)")
        self._set_text(self._ipa_text, f"[{ipa}]" if ipa else "(no IPA available)")

    def _toggle_ipa(self) -> None:
        if self._popup is None or self._ipa_frame is None:
            return
        m = POPUP_MARGIN
        if self._ipa_visible:
            self._ipa_frame.place_forget()
            self._ipa_btn.config(text="▸ IPA")
            self._ipa_visible = False
        else:
            # Top anchored at the window's middle, extending down to just above the
            # button — relheight means it (and the translation) rescale on resize.
            self._ipa_frame.place(relx=0.0, x=m, rely=0.5, y=0, anchor="nw",
                                  relwidth=1.0, width=-2 * m,
                                  relheight=0.5, height=-(self._btn_h + 2 * m))
            self._ipa_btn.lift()
            self._ipa_btn.config(text="▾ IPA")
            self._ipa_visible = True
        self._layout_translation()

    def _layout_translation(self) -> None:
        """Size the translation panel: the top half when the transcription is
        shown (so each takes ~half and both rescale on resize), or the full height
        above the button when collapsed. It never overlaps the transcription."""
        if self._trans_frame is None:
            return
        m = POPUP_MARGIN
        try:
            if self._ipa_visible:
                self._trans_frame.place_configure(relheight=0.5, height=-2 * m)
            else:
                self._trans_frame.place_configure(relheight=1.0, height=-(self._btn_h + 3 * m))
        except Exception:
            pass

    @staticmethod
    def _set_text(widget, content: str) -> None:
        if widget is None:
            return
        try:
            widget.config(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", content)
            widget.config(state="disabled")
        except Exception:
            pass

    @staticmethod
    def _attach_scroll_indicator(parent, text) -> None:
        """Slim scroll-position indicator on the right of a Text: a thin track +
        thumb showing the current position, draggable, wheel-scrollable, and
        hidden when everything fits. Replaces the chunky default scrollbar."""
        track = tk.Frame(parent, bg="#2b2c2e")
        thumb = tk.Frame(track, bg="#5a5b5e")
        thumb.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        shown = {"v": False}

        def update(first, last):
            try:
                first, last = float(first), float(last)
                need = not (first <= 0.0 and last >= 1.0)
                if need and not shown["v"]:
                    track.place(relx=1.0, rely=0.0, anchor="ne", relheight=1.0, width=4)
                    shown["v"] = True
                elif not need and shown["v"]:
                    track.place_forget()
                    shown["v"] = False
                if need:  # move the thumb in place (no re-placement => no flicker)
                    thumb.place_configure(rely=first, relheight=max(0.06, last - first))
            except Exception:
                pass

        text.config(yscrollcommand=update)

        def on_wheel(e):
            text.yview_scroll(int(-e.delta / 120), "units")
            return "break"

        grab = {"off": 0.0}

        def thumb_press(e):
            # Remember where on the thumb the grab started so it doesn't snap its
            # top to the cursor; the drag then moves relative to that point.
            grab["off"] = e.y_root - thumb.winfo_rooty()
            return "break"

        def thumb_drag(e):
            h = track.winfo_height() or 1
            top = (e.y_root - grab["off"]) - track.winfo_rooty()
            text.yview_moveto(min(max(top / h, 0.0), 1.0))
            return "break"

        def track_jump(e):  # clicking the empty track jumps to that spot
            h = track.winfo_height() or 1
            text.yview_moveto(min(max((e.y_root - track.winfo_rooty()) / h, 0.0), 1.0))
            return "break"

        text.bind("<MouseWheel>", on_wheel)
        thumb.bind("<Button-1>", thumb_press)
        thumb.bind("<B1-Motion>", thumb_drag)
        track.bind("<Button-1>", track_jump)
        track.bind("<B1-Motion>", track_jump)

    @staticmethod
    def _install_resize_edges(pop, inner) -> None:
        """Resize by dragging the bottom-right corner — one small invisible
        hit-zone tucked in the corner, so it doesn't form visible edge borders or
        clip the content. The resize cursor on hover is the only hint."""
        st: dict[str, tuple] = {}
        corner = tk.Frame(pop, bg="#202123", cursor="sizing")

        def start(e):
            st["rz"] = (e.x_root, e.y_root, pop.winfo_width(), pop.winfo_height())

        def move(e):
            if "rz" not in st:
                return
            ox, oy, ww, hh = st["rz"]
            pop.geometry(f"{max(240, ww + e.x_root - ox)}x{max(140, hh + e.y_root - oy)}")

        corner.bind("<Button-1>", start)
        corner.bind("<B1-Motion>", move)
        corner.place(relx=1.0, rely=1.0, anchor="se", width=14, height=14)

    def _close_popup(self) -> None:
        # The popup's own [X] / Esc: hide translation, un-gray 🌐, keep toolbar.
        self._destroy_popup()
        self._set_translate_active(False)
        self._refresh_timer()

    def _destroy_popup(self) -> None:
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
        self._popup = None
        self._trans_text = None
        self._ipa_text = None
        self._ipa_frame = None
        self._ipa_btn = None
        self._ipa_visible = False

    def _stop_playback(self) -> None:
        """Stop an in-progress read by terminating the speak subprocess. Because
        playback lives in that child, killing it can't destabilise this UI."""
        proc = self._speak_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _arm_timer(self) -> None:
        self._cancel_timer()
        self._timer = self.root.after(self.timeout_ms, self._destroy_toolbar)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            try:
                self.root.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def _refresh_timer(self) -> None:
        # Auto-dismiss only when idle: nothing speaking and no translation open.
        if self._speaking or self._popup is not None:
            self._cancel_timer()
        else:
            self._arm_timer()

    def _set_read_active(self, active: bool) -> None:
        if self._read_btn is not None:
            try:
                self._read_btn.config(fg=COLOR_BUSY if active else COLOR_IDLE)
            except Exception:
                pass

    def _set_translate_active(self, active: bool) -> None:
        if self._translate_btn is not None:
            try:
                self._translate_btn.config(fg=COLOR_BUSY if active else COLOR_IDLE)
            except Exception:
                pass

    def _close(self) -> None:
        """Cross/Esc: stop reading (if any) and dismiss the toolbar."""
        if self._speaking:
            self._stop_playback()
        self._destroy_toolbar()

    def _destroy_toolbar(self) -> None:
        self._cancel_timer()
        self._destroy_popup()
        if self._toolbar is not None:
            try:
                self._toolbar.destroy()
            except Exception:
                pass
            self._toolbar = None

    def run(self) -> None:
        self.root.mainloop()


def make_mouse_listener(app: SelectionToolbar, window_title_re: str | None,
                        threshold: int) -> mouse.Listener:
    """Build the pynput mouse listener that turns a left-drag into a toolbar."""
    state: dict[str, tuple[int, int, float] | None] = {"press": None}

    def on_click(x, y, button, pressed):  # noqa: ANN001
        if button != mouse.Button.left:
            return
        if pressed:
            state["press"] = (x, y, time.time())
            return
        press = state["press"]
        state["press"] = None
        if press is None:
            return
        # A toolbar is already up — leave further drags alone so we don't fire
        # Ctrl+C / pop over another tool (e.g. a Flameshot screenshot region).
        if app.is_showing():
            return
        if not is_drag((press[0], press[1]), (x, y), threshold):
            return
        if not window_allowed(foreground_window_title(), window_title_re):
            return
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ""
        captured = capture_selection(
            send_ctrl_c, pyperclip.paste, pyperclip.copy, prev, settle=CLIPBOARD_SETTLE_S
        ).strip()
        if not captured:
            return
        logging.info("selection captured (%d chars)", len(captured))
        ax, ay = selection_anchor((press[0], press[1]), (x, y))
        app.request_show(int(ax), int(ay), captured)

    return mouse.Listener(on_click=on_click)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="claude-speech selection toolbar (Read aloud)")
    parser.add_argument("--voice", help="edge-tts voice id (default: from claude_speech.json)")
    parser.add_argument("--output-device", default=None,
                        help="speaker/headphone for playback: index or name substring (default: from claude_speech.json)")
    parser.add_argument("--rate", default=speak_lang.DEFAULT_RATE, help="edge-tts rate, e.g. -10%% or +5%%")
    parser.add_argument("--window-title-re", default=None,
                        help="restrict the toolbar to windows whose title matches this regex. "
                             "Default comes from claude_speech.json's toolbar_window_re, or Claude-only "
                             "if unset. Pass an empty string to allow any application.")
    parser.add_argument("--drag-threshold", type=int, default=DEFAULT_DRAG_THRESHOLD,
                        help=f"pointer travel (px) that counts as a selection (default: {DEFAULT_DRAG_THRESHOLD})")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS,
                        help=f"auto-dismiss the toolbar after this many ms (default: {DEFAULT_TIMEOUT_MS})")
    parser.add_argument("--list-devices", action="store_true",
                        help="print available audio output devices and exit")
    parser.add_argument("--speak", action="store_true",
                        help=argparse.SUPPRESS)  # internal: read text from stdin, speak it, exit
    args = parser.parse_args(argv)

    if args.list_devices:
        print(speak_lang.format_device_list())
        return 0

    # Internal worker mode: the toolbar spawns `... --speak` as a child process
    # and pipes the selected text on stdin. Isolating playback in its own
    # process means interrupting (terminating) it can never crash the UI.
    if args.speak:
        setup_logging()
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        ok = speak_text(text, args.voice, args.output_device, args.rate)
        return 0 if ok else 1

    setup_logging()
    config = load_project_config()
    voice = args.voice or config.get("voice")
    output_device = args.output_device if args.output_device is not None else config.get("output_device")
    target_code = config.get("target_code")
    common_code = config.get("common_code")
    if not voice:
        logging.error("no voice given; pass --voice or run install.py to write %s", CONFIG_PATH.name)
        print(
            f"ERROR: no voice configured. Pass --voice <id>, or run the installer so\n"
            f"       {CONFIG_PATH} records one.",
            file=sys.stderr,
        )
        return 2

    window_re = resolve_window_re(args.window_title_re, config)
    scope = window_re or "any application"
    logging.info("ready: voice=%s, output_device=%s, target=%s, common=%s, scope=%s",
                 voice, output_device, target_code, common_code, scope)
    print("=" * 60, flush=True)
    print("Selection toolbar active.", flush=True)
    print("  Select text (drag) -> 🌐 (translate) / 🔊 (read aloud) buttons appear.", flush=True)
    print(f"  Voice: {voice}. Output: {output_device or 'system default'}. Scope: {scope}.", flush=True)
    print(f"  Translate: {target_code or '?'} -> {common_code or '?'} (offline, argostranslate).", flush=True)
    print("Ctrl+C in this terminal to stop.", flush=True)
    print("=" * 60, flush=True)

    set_dpi_awareness()  # must precede the first Tk() so geometry uses physical px
    app = SelectionToolbar(voice, output_device, args.rate, target_code, common_code,
                           timeout_ms=args.timeout_ms)
    listener = make_mouse_listener(app, window_re, args.drag_threshold)
    listener.start()
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
