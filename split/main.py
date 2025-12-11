#!/usr/bin/env python3
import os, sys, re, json, time, signal, shutil, atexit, subprocess, threading, contextlib
from pathlib import Path

from gpiozero import Button
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QTimer, QRect, QObject, pyqtSignal, pyqtSlot

from hud_tools import (
    HTML_DIR,
    build_url,
    hud_set_words_async,
    hud_ready,
    navigate_hud_to,
    launch_hud_initial,
    hud_set_meters_percent,
)

from hardware_tools import HW, osc_karaoke_on, osc_karaoke_off
from video_tools import (
    init_video,
    play_intro_loop,
    play_video,
    stop_intro_loop,
    detach_end_evt,
    jump_to_tail_and_pause,
    shutdown_video,
)

# =========================
# ====== CONFIG / IO ======
# =========================
LEFT_GPIO   = 26
CENTER_GPIO = 16
RIGHT_GPIO  = 13

MAIN_NAME   = "HDMI-A-2"   # video on main display
SMALL_NAME  = "HDMI-A-1"   # HUD on small display

# Inactivity timeout (ms) while awaiting a choice
INACTIVITY_MS = 90000  # 90 seconds

# Meter ranges / baseline
TEMP_MIN, TEMP_MAX   = 15, 32
MONEY_MIN, MONEY_MAX = 0, 5
BASE_TEMP_C          = 22
BASE_MONEY_STEP      = 4

DEBUG = False
def log(*a):
    if DEBUG:
        print("[DBG]", *a, flush=True)

# Ensure X11 embedding for PyQt
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

BASE_DIR = HTML_DIR.parent
LOLA_DIR = BASE_DIR / "lola"

# =================================================
# ====== Monitor geometry (from video_player) =====
# =================================================
def get_monitor_geos():
    out = subprocess.check_output(["xrandr", "--listmonitors"], text=True).splitlines()[1:]
    geos = {}
    for line in out:
        parts = line.split()
        name = parts[-1]
        tok = next((t for t in parts if 'x' in t and '+' in t), None)
        if not tok:
            continue
        m = re.search(r'(\d+)(?:/\d+)?x(\d+)(?:/\d+)?\+(\d+)\+(\d+)', tok)
        if not m:
            continue
        w, h, x, y = map(int, m.groups())
        geos[name] = QRect(x, y, w, h)
    return geos

mon = get_monitor_geos()
if MAIN_NAME not in mon or SMALL_NAME not in mon:
    print("Couldn't find required monitors. Found:", list(mon.keys()))
    sys.exit(1)

MAIN_RECT  = mon[MAIN_NAME]
SMALL_RECT = mon[SMALL_NAME]

# =============================
# ======= Qt / Window =========
# =============================
app = QApplication(sys.argv)

# -------- Main-thread gate to route work to Qt thread --------
class _MainThreadGate(QObject):
    call = pyqtSignal(object)
    def __init__(self):
        super().__init__()
        # Explicit queued connection to ensure cross-thread delivery
        self.call.connect(self._on_call, Qt.QueuedConnection)
    @pyqtSlot(object)
    def _on_call(self, fn):
        try:
            fn()
        except Exception as e:
            print(f"[ERR] gate call failed: {e}", flush=True)

_gate = _MainThreadGate()

def _call_on_main(fn):
    # If already on main thread, run immediately; otherwise queue to Qt thread
    if threading.current_thread() is threading.main_thread():
        fn()
    else:
        _gate.call.emit(fn)

# ------------------------------------------------------------

def make_window(rect: QRect, bypass_wm: bool):
    flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    if bypass_wm: flags |= Qt.X11BypassWindowManagerHint
    w = QWidget(None, flags)
    w.setAttribute(Qt.WA_NativeWindow, True)
    w.setGeometry(rect); w.move(rect.topLeft()); w.resize(rect.size())
    w.setStyleSheet("background-color:#000;")
    w.show(); w.raise_()
    return w

main_w = make_window(MAIN_RECT, bypass_wm=True)
main_xid = int(main_w.winId())

# =============================
# ======= State Machine =======
# =============================

# Load STATES from external JSON
STATE_PATH = BASE_DIR / "states.json"
with STATE_PATH.open("r", encoding="utf-8") as f:
    STATES = json.load(f)

SPRITES = {
    "base":        HTML_DIR / "base_jelly.html",
    "stinging":    HTML_DIR / "stinging_jelly.html",
    "mining":      HTML_DIR / "mining_jelly.html",
    "overheating": HTML_DIR / "overheating_jelly.html",
    "cooling":     HTML_DIR / "cooling_jelly.html",
    "singing":     HTML_DIR / "singing_jelly.html",
    "broke":       HTML_DIR / "broke_jelly.html",
}

def ensure_sprite(p: Path):
    if not p.exists():
        raise FileNotFoundError(f"Missing sprite HTML: {p}")

current_state = None
phase = "playing"  # "playing" or "awaiting_choice"

_inactivity_timer = None
current_temp_c = BASE_TEMP_C
current_money_step = BASE_MONEY_STEP

# Validate assets exist
for sid, s in STATES.items():
    vpath = LOLA_DIR / s["video"]
    if not vpath.exists() and sid != "0":
        print(f"Warning: missing video file {vpath}", file=sys.stderr)
for key, p in SPRITES.items():
    ensure_sprite(p)

# =============================
# ======= Inactivity timer ====
# =============================
def _ensure_inactivity_timer():
    global _inactivity_timer
    if _inactivity_timer is None:
        t = QTimer(); t.setSingleShot(True); t.setInterval(INACTIVITY_MS)
        t.timeout.connect(_on_inactivity_timeout)
        _inactivity_timer = t
    return _inactivity_timer

def _on_inactivity_timeout():
    print("[INACTIVITY] 90s with no choice press -> restart intro loop", flush=True)
    _restart_loop_0()

def _start_inactivity_timer():
    timer = _ensure_inactivity_timer()
    if timer.isActive(): timer.stop()
    log("Inactivity timer: START"); timer.start()

def _cancel_inactivity_timer():
    timer = _ensure_inactivity_timer()
    if timer.isActive():
        log("Inactivity timer: CANCEL"); timer.stop()

def _bump_inactivity_timer():
    log("Inactivity timer: BUMP"); _start_inactivity_timer()

# ---- Safe wrappers to always run on Qt (GUI) thread ----
def start_inactivity():  _call_on_main(_start_inactivity_timer)
def cancel_inactivity(): _call_on_main(_cancel_inactivity_timer)
def bump_inactivity():   _call_on_main(_bump_inactivity_timer)
# --------------------------------------------------------

# =============================
# ======= Meter helpers =======
# =============================
def money_step_to_pct(step: int) -> int:
    step = max(MONEY_MIN, min(MONEY_MAX, int(step)))
    return int(round((step / float(MONEY_MAX)) * 100))

def temp_c_to_pct(c: int) -> int:
    c = max(TEMP_MIN, min(TEMP_MAX, int(c)))
    pct = ((c - TEMP_MIN) / float(TEMP_MAX - TEMP_MIN)) * 100.0
    return int(round(max(0.0, min(100.0, pct))))

# =============================
# ======= State Routines ======
# =============================
def _set_phase(new_phase):
    global phase
    phase = new_phase; log("Phase ->", phase)

def _apply_and_push_meters(temp_c, money_step):
    tc = max(TEMP_MIN,  min(TEMP_MAX,  int(temp_c)))
    ms = max(MONEY_MIN, min(MONEY_MAX, int(money_step)))
    hud_ready()
    hud_set_meters_percent(temp_c_to_pct(tc), money_step_to_pct(ms))
    return tc, ms

_last_video_was_skipped = False  # track skip vs natural end

def _show_choice_labels():
    """Reveal labels and arm inactivity (keep paused frame on screen)."""
    global _playing
    # Hardware post hooks (run once per video end or skip)
    try:
        if current_state:
            HW.post_for_state(current_state, _last_video_was_skipped)
    except Exception as e:
        log("post_for_state error:", e)

    _set_phase("awaiting_choice")
    s = STATES[current_state]
    end_words = s.get("end_words") or ["", "", ""]
    transitions = s.get("transitions", {})
    # Arm inactivity ONLY if there is at least one actionable choice
    has_choice = any([
        end_words[0] and ("L" in transitions),
        end_words[1] and ("C" in transitions),
        end_words[2] and ("R" in transitions),
    ])
    hud_set_words_async(end_words)
    print(f"[CHOICE] labels={tuple(end_words)} actionable={has_choice}", flush=True)
    if has_choice:
        start_inactivity()
    else:
        cancel_inactivity()

def _post_video_triggers():
    """
    Things that should happen when a video is considered 'done'
    (either natural end or skip).
    """
    # Karaoke OFF when 6_1_1 ends or is skipped
    if current_state == "6_1_1":
        osc_karaoke_off()

def _restart_loop_0():
    cancel_inactivity()
    enter_state("0")

def enter_state(state_id: str):
    global current_state, current_temp_c, current_money_step
    if state_id == "RESTART":
        _restart_loop_0()
        return

    if state_id not in STATES:
        print(f"[ERR] Unknown state: {state_id}", file=sys.stderr)
        return

    s = STATES[state_id]
    cancel_inactivity()  # never during playback

    sprite = SPRITES[s["sprite"]]
    initial_words = s["during_words"] if state_id != "0" else ["Begin", "", ""]
    navigate_hud_to(sprite, initial_words=initial_words)

    if state_id == "0":
        new_temp_c, new_money_step = BASE_TEMP_C, BASE_MONEY_STEP
    else:
        dt, dm = int(s.get("temp_delta", 0)), int(s.get("money_delta", 0))
        new_temp_c     = current_temp_c + dt
        new_money_step = current_money_step + dm

    def _after():
        tc, ms = _apply_and_push_meters(new_temp_c, new_money_step)
        globals()['current_temp_c'] = tc
        globals()['current_money_step'] = ms
    threading.Thread(target=_after, daemon=True).start()

    # Hardware pre hooks just before the video launches
    HW.reset_post_guard()
    HW.pre_for_state(state_id)

    print(f"[STATE] -> {state_id}", flush=True)
    if state_id == "0":
        main_w.setStyleSheet("background-color:#000;")
        play_intro_loop(str(LOLA_DIR), STATES["0"]["video"])
    else:
        main_w.setStyleSheet("background-color:#000;")
        play_video(str(LOLA_DIR), s["video"])
        # When the 6_1_1 sequence is triggered, send /karaoke 1
        if state_id == "6_1_1":
            osc_karaoke_on()

    current_state = state_id
    _set_phase("playing")

def on_video_end():
    global _last_video_was_skipped
    if current_state == "0":
        return
    if phase != "playing":
        return
    _last_video_was_skipped = False   # natural end
    _post_video_triggers()           # same triggers as skip
    _show_choice_labels()

# =======================================================
# ===== SKIP logic – CLEAN JUMP TO FINAL FRAME (VLC) ====
# =======================================================
def _skip_to_last_frame_and_choice():
    if current_state == "0":
        return
    detach_end_evt()
    try:
        jump_to_tail_and_pause()
    except Exception as e:
        log("skip_to_last_frame error:", e)
    _show_choice_labels()

def skip_current():
    global _last_video_was_skipped
    if current_state == "0":
        return
    if phase == "playing":
        _last_video_was_skipped = True  # drives post-action rules
        _post_video_triggers()         # ensure "after-finish" triggers also run on skip
        _skip_to_last_frame_and_choice()

# ==================================
# ======= Button Interactions ======
# ==================================
btn_left   = Button(LEFT_GPIO,   pull_up=True, bounce_time=0.1)
btn_center = Button(CENTER_GPIO, pull_up=True, bounce_time=0.1)
btn_right  = Button(RIGHT_GPIO,  pull_up=True, bounce_time=0.1)

def handle_left():
    global phase
    if current_state == "0":
        enter_state(STATES["0"]["transitions"]["L"])
        return
    if phase == "playing":
        skip_current()
        return
    bump_inactivity()
    s = STATES[current_state]
    end_words = s.get("end_words") or ["", "", ""]
    label = end_words[0]
    if not label:
        return
    nxt = s.get("transitions", {}).get("L")
    if not nxt:
        return
    if nxt == "RESTART":
        _restart_loop_0()
    else:
        enter_state(nxt)

def handle_center():
    global phase
    if phase != "awaiting_choice":
        return
    bump_inactivity()
    s = STATES[current_state]
    end_words = s.get("end_words") or ["", "", ""]
    label = end_words[1]
    if not label:
        return
    nxt = s.get("transitions", {}).get("C")
    if not nxt:
        return
    if nxt == "RESTART":
        _restart_loop_0()
    else:
        enter_state(nxt)

def handle_right():
    global phase
    if phase == "playing":
        return
    bump_inactivity()
    s = STATES.get(current_state, {})
    end_words = s.get("end_words") or ["", "", ""]
    label = end_words[2]
    if label == "Restart":
        _restart_loop_0()

btn_left.when_pressed   = handle_left
btn_center.when_pressed = handle_center
btn_right.when_pressed  = handle_right

# ========================
# ====== Lifecycle =======
# ========================
CHROME_PROC = None
_shutting_down = False

def _quit(*_sig):
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True

    # Hardware cleanup first
    with contextlib.suppress(Exception): HW.shutdown()

    # Video cleanup
    with contextlib.suppress(Exception): stop_intro_loop()
    with contextlib.suppress(Exception): shutdown_video()

    with contextlib.suppress(Exception): btn_left.close()
    with contextlib.suppress(Exception): btn_center.close()
    with contextlib.suppress(Exception): btn_right.close()

    try:
        if CHROME_PROC and CHROME_PROC.poll() is None:
            try:
                os.killpg(os.getpgid(CHROME_PROC.pid), signal.SIGTERM)
                time.sleep(0.3)
                if CHROME_PROC.poll() is None:
                    os.killpg(os.getpgid(CHROME_PROC.pid), signal.SIGKILL)
            except Exception:
                with contextlib.suppress(Exception): CHROME_PROC.terminate()
                with contextlib.suppress(Exception): CHROME_PROC.kill()
    except Exception:
        pass

    with contextlib.suppress(Exception):
        QTimer.singleShot(0, app.quit)
    threading.Timer(0.6, lambda: os._exit(0)).start()

signal.signal(signal.SIGINT, _quit)
signal.signal(signal.SIGTERM, _quit)
atexit.register(_quit)

# Keep event loop alive
tick = QTimer()
tick.timeout.connect(lambda: None)
tick.start(100)
app.aboutToQuit.connect(_quit)

# ============ Startup ============
# Initialize VLC / video layer with callback
init_video(main_xid, on_video_end)

# Launch HUD window and start intro state
CHROME_PROC = launch_hud_initial(SPRITES["base"], SMALL_RECT)
enter_state("0")  # Start baseline + loop 0.mkv

sys.exit(app.exec_())
