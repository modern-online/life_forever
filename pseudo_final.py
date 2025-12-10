#!/usr/bin/env python3
# deterministic 3-button video/sprite controller with CDP HUD labels
# Only 0.mkv loops (at launch, explicit Restart, or 90s inactivity on choice screen).
# Never restart during playback.
# SKIP (during non-zero videos) jumps to the exact last frame and freezes there, then shows the menu.

import os, sys, re, json, time, signal, shutil, atexit, subprocess, urllib.request, urllib.parse, threading, contextlib
from pathlib import Path

# ==== Imports for main-screen video ====
import vlc
from gpiozero import Button
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QTimer, QRect, QObject, pyqtSignal, pyqtSlot

# =========================
# ====== CONFIG / IO ======
# =========================
LEFT_GPIO   = 26
CENTER_GPIO = 16
RIGHT_GPIO  = 13

MAIN_NAME   = "HDMI-A-2"   # video on main display
SMALL_NAME  = "HDMI-A-1"   # HUD on small display
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LOLA_DIR    = os.path.join(BASE_DIR, "lola")

HTML_DIR     = Path(__file__).resolve().parent / "jellies"
DEFAULT_FILE = "base_jelly.html"

# Chromium & CDP
REMOTE_PORT  = 9222
PROFILE_DIR  = HTML_DIR / ".jelly_profile"
PROFILE_DIR.mkdir(exist_ok=True)

# Inactivity timeout (ms) while awaiting a choice
INACTIVITY_MS = 90000  # 90 seconds

# Meter ranges / baseline
TEMP_MIN, TEMP_MAX   = 15, 32
MONEY_MIN, MONEY_MAX = 0, 5
BASE_TEMP_C          = 22
BASE_MONEY_STEP      = 3

DEBUG = False
def log(*a):
    if DEBUG:
        print("[DBG]", *a, flush=True)

# Ensure X11 embedding for PyQt
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

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

# ===============================================
# =============== Chromium / CDP ================
# ===============================================
CHROMIUM = os.environ.get("CHROMIUM") or os.environ.get("CHROMIUM_BIN")
for c in (CHROMIUM, "chromium", "chromium-browser", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
    if c and (shutil.which(c) or Path(c).exists()):
        CHROMIUM = c; break
if not CHROMIUM:
    print("Chromium not found. Set CHROMIUM=/path/to/chromium", file=sys.stderr); sys.exit(1)

try:
    import websocket
except Exception:
    print("Missing dependency 'websocket-client'", file=sys.stderr); sys.exit(1)

def _http_get(url, timeout=1.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8")

def _targets():
    return json.loads(_http_get(f"http://127.0.0.1:{REMOTE_PORT}/json", 1.0))

def _json_version():
    return json.loads(_http_get(f"http://127.0.0.1:{REMOTE_PORT}/json/version", 1.0))

def _pick_target():
    ts = _targets()
    for t in ts:
        if t.get("type") in ("page","app") and str(t.get("url","")).startswith("file://"):
            return t
    for t in ts:
        if t.get("type") in ("page","app"):
            return t
    return None

class CDPPage:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=3.0); self.i = 0
    def _send(self, method, params=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self.i:
                return resp
    def enable_page(self): self._send("Page.enable")
    def nav(self, url):
        self.enable_page(); self._send("Page.navigate", {"url": url})
    def front(self): self._send("Page.bringToFront")
    def enable_runtime(self): self._send("Runtime.enable")
    def eval_js(self, expression, await_promise=True):
        self.enable_runtime()
        return self._send("Runtime.evaluate", {"expression": expression,"awaitPromise": True,"returnByValue": True})
    def close(self):
        with contextlib.suppress(Exception): self.ws.close()

class CDPBrowser:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=3.0); self.i = 0
    def _send(self, method, params=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self.i:
                return resp
    def get_window_for_target(self, targetId):
        return self._send("Browser.getWindowForTarget", {"targetId": targetId})
    def set_window_bounds(self, windowId, left, top, width, height, state="normal"):
        params = {"windowId": windowId, "bounds": {"left": left, "top": top, "width": width, "height": height, "windowState": state}}
        return self._send("Browser.setWindowBounds", params)
    def close(self):
        with contextlib.suppress(Exception): self.ws.close()

def _connect_page_cdp(wait=10.0):
    t0 = time.time(); last = None
    while time.time()-t0 < wait:
        try:
            tgt = _pick_target()
            if tgt and "webSocketDebuggerUrl" in tgt:
                return CDPPage(tgt["webSocketDebuggerUrl"]), tgt
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"CDP(page) connect failed: {last}")

def _connect_browser_cdp(wait=10.0):
    t0 = time.time(); last = None
    while time.time()-t0 < wait:
        try:
            ver = _json_version()
            if "webSocketDebuggerUrl" in ver:
                return CDPBrowser(ver["webSocketDebuggerUrl"])
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"CDP(browser) connect failed: {last}")

def _cdp_eval_js(js_code, tries=30, sleep_s=0.1):
    last_err = None
    for _ in range(tries):
        try:
            page_cdp, _ = _connect_page_cdp(5.0)
            try: page_cdp.eval_js(js_code); return True
            finally: page_cdp.close()
        except Exception as e:
            last_err = e; time.sleep(sleep_s)
    if DEBUG: print("CDP eval failed:", last_err, file=sys.stderr)
    return False

def build_url(p: Path, words=("","","")) -> str:
    params = {"w1": words[0] or "", "w2": words[1] or "", "w3": words[2] or ""}
    qs = urllib.parse.urlencode(params, doseq=False, safe="")
    return f"file://{p}?{qs}"

def hud_set_words(words):
    esc = lambda s: (s or "").replace("\\","\\\\").replace("'","\\'")
    js = f"window.JellyHUD && JellyHUD.setWords(['{esc(words[0])}','{esc(words[1])}','{esc(words[2])}']);"
    _cdp_eval_js(js)

def hud_set_words_async(words):
    threading.Thread(target=lambda: hud_set_words(words), daemon=True).start()

def hud_ready():
    return _cdp_eval_js("typeof window.JellyHUD !== 'undefined' ? true : (function(){return true;})();")

def navigate_hud_to(sprite_path: Path, initial_words=("","","")):
    def worker():
        try:
            page_cdp, _tgt = _connect_page_cdp(5.0)
            try:
                url = build_url(sprite_path, words=("", "", ""))
                url += ("&" if "?" in url else "?") + f"t={int(time.time()*1000)}"
                page_cdp.nav(url); page_cdp.front()
            finally:
                page_cdp.close()
            hud_ready(); hud_set_words(initial_words)
        except Exception as e:
            log("navigate_hud_to error:", e)
    threading.Thread(target=worker, daemon=True).start()

# ===== Meter helpers =====
def hud_set_meters_percent(temp_pct: int, money_pct: int):
    js = f"window.JellyHUD && JellyHUD.set({int(temp_pct)}, {int(money_pct)});"
    _cdp_eval_js(js)

def money_step_to_pct(step: int) -> int:
    step = max(MONEY_MIN, min(MONEY_MAX, int(step)))
    return int(round((step / float(MONEY_MAX)) * 100))

def temp_c_to_pct(c: int) -> int:
    c = max(TEMP_MIN, min(TEMP_MAX, int(c)))
    pct = ((c - TEMP_MIN) / float(TEMP_MAX - TEMP_MIN)) * 100.0
    return int(round(max(0.0, min(100.0, pct))))

# =============================
# ======= Video / Window ======
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

# ===== VLC (QUIET) =====
inst = vlc.Instance("--no-video-title-show", "--quiet", "--verbose=-1")
player = inst.media_player_new()
player.set_xwindow(main_xid)

# Event manager for non-zero videos only
em = player.event_manager()
_end_evt_attached = False
def _evt_end(event=None):
    print(f"[END] state={current_state} file={_current_media_name}", flush=True)
    on_video_end()

def _attach_end_evt():
    global _end_evt_attached
    if not _end_evt_attached:
        em.event_attach(vlc.EventType.MediaPlayerEndReached, _evt_end)
        _end_evt_attached = True

def _detach_end_evt():
    global _end_evt_attached
    if _end_evt_attached:
        with contextlib.suppress(Exception):
            em.event_detach(vlc.EventType.MediaPlayerEndReached, _evt_end)
        _end_evt_attached = False

# ===== Intro loop (0.mkv) via MediaListPlayer =====
_intro_ml = None
_intro_mlp = None

def _stop_intro_loop():
    global _intro_mlp, _intro_ml
    if _intro_mlp:
        with contextlib.suppress(Exception): _intro_mlp.stop()
        with contextlib.suppress(Exception): _intro_mlp.release()
    if _intro_ml:
        with contextlib.suppress(Exception): _intro_ml.release()
    _intro_mlp = None
    _intro_ml  = None

def _play_intro_loop():
    """Fresh MediaListPlayer for 0.mkv every time."""
    global _intro_mlp, _intro_ml, _playing, _looping, _current_media_name
    _detach_end_evt()
    with contextlib.suppress(Exception): player.stop()
    with contextlib.suppress(Exception): player.set_media(None)
    with contextlib.suppress(Exception): player.set_xwindow(main_xid)

    _stop_intro_loop()
    _intro_mlp = inst.media_list_player_new()
    _intro_mlp.set_media_player(player)
    v0 = os.path.join(LOLA_DIR, STATES["0"]["video"])
    _intro_ml = inst.media_list_new([v0])
    _intro_mlp.set_media_list(_intro_ml)
    _intro_mlp.set_playback_mode(vlc.PlaybackMode.loop)

    print("[PLAY] 0.mkv via fresh MediaListPlayer", flush=True)
    main_w.setStyleSheet("background-color:#000;")
    _intro_mlp.play()

    _playing = True
    _looping = True
    _current_media_name = STATES["0"]["video"]

# =============================
# ======= State Machine =======
# =============================
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

STATES = {
    "0": {
        "video": "0.mkv",
        "sprite": "base",
        "during_words": ("Begin","",""),
        "end_words": None,
        "transitions": {"L": "1"},
    },
    "1": {
        "video": "1.mkv",
        "sprite": "base",
        "during_words": ("SKIP","",""),
        "end_words": ("Pleasure","","Restart"),
        "transitions": {"L": "2", "R": "RESTART"},
        "money_delta": +0,
        "temp_delta":  +1,
    },
    "2": {
        "video": "2.mkv",
        "sprite": "stinging",
        "during_words": ("SKIP","",""),
        "end_words": ("Money","Cooldown","Restart"),
        "transitions": {"L": "3_1_1", "C": "3_2_1", "R": "RESTART"},
        "money_delta": +1,
        "temp_delta":  +2,
    },
    "3_1_1": {
        "video": "3_1_1.mkv",
        "sprite": "mining",
        "during_words": ("SKIP","",""),
        "end_words": ("Pleasure","Cooldown","Restart"),
        "transitions": {"L": "4_1_1", "C": "4_1_2", "R": "RESTART"},
        "money_delta": +2,
        "temp_delta":  -1,
    },
    "4_1_1": {
        "video": "4_1_1.mkv",
        "sprite": "stinging",
        "during_words": ("SKIP","",""),
        "end_words": ("Pleasure","Cooldown","Restart"),
        "transitions": {"L": "5_1_1", "C": "4_1_2", "R": "RESTART"},
        "money_delta": -1,
        "temp_delta":  +3,
    },
    "5_1_1": {
        "video": "5_1_1.mkv",
        "sprite": "overheating",
        "during_words": ("SKIP","",""),
        "end_words": ("","","Restart"),
        "transitions": {"R": "RESTART"},
        "money_delta": -2,
        "temp_delta":  +6,
    },
    "4_1_2": {
        "video": "4_1_2.mkv",
        "sprite": "cooling",
        "during_words": ("SKIP","",""),
        "end_words": ("Sing","","Restart"),
        "transitions": {"L": "6_1_1", "R": "RESTART"},
        "money_delta": -2,
        "temp_delta":  -8,
    },
    "6_1_1": {
        "video": "6_1_1.mkv",
        "sprite": "singing",
        "during_words": ("","",""),
        "end_words": ("","","Restart"),
        "transitions": {"R": "RESTART"},
        "money_delta":  0,
        "temp_delta":   -1,
    },
    "3_2_1": {
        "video": "3_2_1.mkv",
        "sprite": "cooling",
        "during_words": ("SKIP","",""),
        "end_words": ("Money","Pleasure","Restart"),
        "transitions": {"L": "3_1_1", "C": "4_2_1", "R": "RESTART"},
        "money_delta": +0,
        "temp_delta":  -3,
    },
    "4_2_1": {
        "video": "4_2_1.mkv",
        "sprite": "broke",
        "during_words": ("SKIP","",""),
        "end_words": ("","","Restart"),
        "transitions": {"R": "RESTART"},
        "money_delta": -3,
        "temp_delta":  -4,
    },
}

# Validate assets exist
for sid, s in STATES.items():
    vpath = os.path.join(LOLA_DIR, s["video"])
    if not os.path.exists(vpath) and sid != "0":
        print(f"Warning: missing video file {vpath}", file=sys.stderr)
for key, p in SPRITES.items():
    ensure_sprite(p)

# Playback control
_playing = False
_looping = False
_current_media_name = None

def _play_video(filename: str):
    """Play a single, non-looping video (only 0.mkv loops via MediaListPlayer)."""
    global _playing, _looping, _current_media_name
    vpath = os.path.join(LOLA_DIR, filename)
    if not os.path.exists(vpath):
        print(f"[ERR] Video not found: {vpath}", file=sys.stderr)
        with contextlib.suppress(Exception): player.stop()
        main_w.setStyleSheet("background-color:#000;"); main_w.update()
        _playing = False; _looping = False; _current_media_name = None
        return

    _stop_intro_loop()

    _detach_end_evt()
    with contextlib.suppress(Exception): player.stop()
    with contextlib.suppress(Exception): player.set_media(None)
    with contextlib.suppress(Exception): player.set_xwindow(main_xid)

    main_w.setStyleSheet("background-color:#000;")

    m = inst.media_new(vpath)
    player.set_media(m)
    print(f"[PLAY] normal {filename}", flush=True)
    player.play()

    _attach_end_evt()

    _playing = True
    _looping = False
    _current_media_name = filename
    main_w.update()

def _restart_loop_0():
    cancel_inactivity()
    enter_state("0")

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
# ======= Hardware helpers ====
# =============================

# Optional deps; degrade gracefully if missing
try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

try:
    import paramiko
except Exception:
    paramiko = None

# ======= Hardware IO Config =======
# Arduino on RPi will usually be /dev/ttyACM0 or /dev/ttyUSB0. Auto-pick if not set.
SERIAL_PORT = os.environ.get("ARDUINO_PORT") or None  # None = auto-select
SERIAL_BAUD = int(os.environ.get("ARDUINO_BAUD", "9600"))
SERIAL_INTER = float(os.environ.get("ARDUINO_INTER", "0.05"))  # gap between multi-char sends

# Miner credentials (env overrides supported; defaults match your example)
MINER1_IP   = os.environ.get("MINER1_IP",   "10.162.142.177")
MINER1_USER = os.environ.get("MINER1_USER", "root")
MINER1_PW   = os.environ.get("MINER1_PW",   "lifeforever")
MINER2_IP   = os.environ.get("MINER2_IP",   "10.162.142.113")
MINER2_USER = os.environ.get("MINER2_USER", "root")
MINER2_PW   = os.environ.get("MINER2_PW",   "lifeforever")

class SerialBridge:
    """Tiny helper around pyserial. Non-blocking sends via short-lived threads."""
    def __init__(self, port=None, baud=9600, inter=0.05):
        self.port_cfg = port
        self.baud = baud
        self.inter = inter
        self.ser = None
        self.lock = threading.Lock()
        if serial is None:
            print("[WARN] pyserial not installed; serial actions disabled.", flush=True)
            return
        try:
            self.open()
        except Exception as e:
            print(f"[WARN] Serial open failed: {e}. Continuing without serial.", flush=True)

    def _choose_port(self):
        if self.port_cfg:
            return self.port_cfg
        if list_ports is None:
            return None
        ports = list(list_ports.comports())
        if not ports:
            return None
        # Prefer devices that look like Arduino/USB serial
        for p in ports:
            desc = (p.description or "").lower()
            dev  = (p.device or "").lower()
            if any(k in desc or k in dev for k in (
                "arduino", "wchusbserial", "usbserial", "usbmodem",
                "ttyacm", "ttyusb", "cu.usbmodem", "cu.usbserial"
            )):
                return p.device
        return ports[0].device  # fallback

    def open(self):
        if serial is None:
            return
        port = self._choose_port()
        if not port:
            print("[WARN] No serial port found; serial actions disabled.", flush=True)
            return
        self.ser = serial.Serial(port, baudrate=self.baud, timeout=0)
        time.sleep(2.5)  # Arduino reset after port open
        with contextlib.suppress(Exception): self.ser.reset_input_buffer()
        with contextlib.suppress(Exception): self.ser.reset_output_buffer()
        print(f"[SERIAL] Connected on {port} @ {self.baud}", flush=True)

    def is_ready(self):
        return (self.ser is not None) and self.ser.is_open

    def _send_bytes(self, b):
        if not self.is_ready():
            return
        with self.lock:
            try:
                self.ser.write(b)
                self.ser.flush()
            except Exception as e:
                print(f"[WARN] Serial send failed: {e}", flush=True)

    def send(self, payload):
        if not payload:
            return
        if isinstance(payload, str):
            b = payload.encode("utf-8")
        else:
            b = bytes(payload)
        print(f"[SERIAL] -> {repr(payload)}", flush=True)
        self._send_bytes(b)

    def send_seq(self, seq, inter=None):
        inter = self.inter if inter is None else inter
        def worker():
            for item in seq:
                self.send(item)
                time.sleep(inter)
        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        if self.ser:
            with contextlib.suppress(Exception): self.ser.close()
            self.ser = None


class MinerController:
    """Lazily connects via SSH and runs start/stop; non-blocking (threaded)."""
    def __init__(self, ip1, user1, pw1, ip2, user2, pw2):
        self.targets = [
            {"ip": ip1, "user": user1, "pw": pw1, "client": None, "name": "miner1"},
            {"ip": ip2, "user": user2, "pw": pw2, "client": None, "name": "miner2"},
        ]
        if paramiko is None:
            print("[WARN] paramiko not installed; miner actions disabled.", flush=True)

    def _connect_one(self, t):
        if paramiko is None:
            return
        if t["client"]:
            return
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(t["ip"], username=t["user"], password=t["pw"], timeout=8)
            t["client"] = c
            print(f"[MINER] Connected to {t['name']} ({t['ip']})", flush=True)
        except Exception as e:
            print(f"[WARN] Miner connect failed for {t['ip']}: {e}", flush=True)

    def _exec_one(self, t, cmd):
        if paramiko is None:
            return
        try:
            if not t["client"]:
                self._connect_one(t)
            if t["client"]:
                t["client"].exec_command(cmd)
                print(f"[MINER] {t['name']}: {cmd}", flush=True)
        except Exception as e:
            print(f"[WARN] Miner cmd failed on {t['ip']}: {e}", flush=True)

    def start_both(self):
        def worker():
            for t in self.targets:
                self._exec_one(t, "/etc/init.d/bosminer start")
        threading.Thread(target=worker, daemon=True).start()

    def stop_both(self):
        def worker():
            for t in self.targets:
                self._exec_one(t, "/etc/init.d/bosminer stop")
        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        for t in self.targets:
            if t.get("client"):
                with contextlib.suppress(Exception): t["client"].close()
                t["client"] = None


class HardwareOrchestrator:
    """
    Maps states to pre/post hardware actions.
    - pre_for_state() runs immediately before a video starts (or loop 0.mkv starts on first entry)
    - post_for_state() runs once after a video *finishes or is skipped* (with skip-awareness)
    """
    def __init__(self):
        self.serial = SerialBridge(SERIAL_PORT, SERIAL_BAUD, inter=SERIAL_INTER)
        self.miners = MinerController(
            MINER1_IP, MINER1_USER, MINER1_PW,
            MINER2_IP, MINER2_USER, MINER2_PW
        )
        self._last_post_done_state = None
        self._did_initial_zero = False  # to stop miners only before the very first 0.mkv loop

    def pre_for_state(self, sid: str):
        # (1) Stop miners once + send 'c' before the very first 0.mkv loop (initial boot only)
        if sid == "0":
            if not self._did_initial_zero:
                self.miners.stop_both()  # best-effort; non-blocking
                self._did_initial_zero = True
            self.serial.send_seq(["f", "c"])

        # (2) v before 1.mkv
        elif sid == "1":
            self.serial.send_seq(["v"])

        # (3) o before 2.mkv
        elif sid == "2":
            self.serial.send_seq(["o"])

        # (4) miners ON before 3_1_1.mkv
        elif sid == "3_1_1":
            self.miners.start_both()

        # (5) b then z before 3_2_1.mkv
        elif sid == "3_2_1":
            self.serial.send_seq(["b", "z"])

        # (6) p before 4_1_1.mkv
        elif sid == "4_1_1":
            self.serial.send_seq(["p"])

        # (7) b then z before 4_1_2.mkv
        elif sid == "4_1_2":
            self.serial.send_seq(["b", "z"])

        # (8) f before 4_2_1.mkv
        elif sid == "4_2_1":
            self.serial.send_seq(["f"])

        # (9) r before 5_1_1.mkv
        elif sid == "5_1_1":
            self.serial.send_seq(["r"])

        # (10) d before 6_1_1.mkv
        elif sid == "6_1_1":
            self.serial.send_seq(["d"])

    def post_for_state(self, sid: str, was_skipped: bool):
        # Guard to ensure we run only once per completed/skimmed video
        if sid == self._last_post_done_state:
            return
        self._last_post_done_state = sid

        # (4) miners OFF when 3_1_1 finishes or is skipped
        if sid == "3_1_1":
            self.miners.stop_both()

        # (5) x after 3_2_1 finishes or is skipped
        elif sid == "3_2_1":
            self.serial.send_seq(["x"])

        # (9) f after 5_1_1 finishes or is skipped
        elif sid == "5_1_1":
            self.serial.send_seq(["f"])

        # (10) f and x after 6_1_1 finishes (NOT on skip)
        elif sid == "6_1_1":
            if not was_skipped:
                self.serial.send_seq(["f", "x"])

    def reset_post_guard(self):
        self._last_post_done_state = None

    def shutdown(self):
        # Best-effort cleanup (non-blocking)
        with contextlib.suppress(Exception): self.miners.stop_both()
        with contextlib.suppress(Exception): self.miners.close()
        with contextlib.suppress(Exception): self.serial.close()


# Single global orchestrator
HW = HardwareOrchestrator()

# =============================
# ======= State Routines ======
# =============================
def _set_phase(new_phase):
    global phase
    phase = new_phase; log("Phase ->", phase)

def _apply_and_push_meters(temp_c, money_step):
    tc = max(TEMP_MIN,  min(TEMP_MAX,  int(temp_c)))
    ms = max(MONEY_MIN, min(MONEY_MAX, int(money_step)))
    hud_ready(); hud_set_meters_percent(temp_c_to_pct(tc), money_step_to_pct(ms))
    return tc, ms

_last_video_was_skipped = False  # track skip vs natural end for post rules

def _show_choice_labels():
    """Reveal labels and arm inactivity (keep paused frame on screen)."""
    global _playing

    # Hardware post hooks (run once per video end or skip)
    try:
        if current_state:
            HW.post_for_state(current_state, _last_video_was_skipped)
    except Exception as e:
        log("post_for_state error:", e)

    _playing = False
    _set_phase("awaiting_choice")
    s = STATES[current_state]
    end_words = s.get("end_words") or ("","","")
    transitions = s.get("transitions", {})
    # Arm inactivity ONLY if there is at least one actionable choice
    has_choice = any([
        end_words[0] and ("L" in transitions),
        end_words[1] and ("C" in transitions),
        end_words[2] and ("R" in transitions),
    ])
    hud_set_words_async(end_words)
    print(f"[CHOICE] labels={end_words} actionable={has_choice}", flush=True)
    if has_choice:
        start_inactivity()
    else:
        cancel_inactivity()

def enter_state(state_id: str):
    global current_state, current_temp_c, current_money_step
    if state_id == "RESTART": return _restart_loop_0()
    if state_id not in STATES:
        print(f"[ERR] Unknown state: {state_id}", file=sys.stderr); return
    s = STATES[state_id]

    cancel_inactivity()  # never during playback

    sprite = SPRITES[s["sprite"]]
    initial_words = s["during_words"] if state_id != "0" else ("Begin","","")
    navigate_hud_to(sprite, initial_words=initial_words)

    if state_id == "0":
        new_temp_c, new_money_step = BASE_TEMP_C, BASE_MONEY_STEP
    else:
        dt, dm = int(s.get("temp_delta", 0)), int(s.get("money_delta", 0))
        new_temp_c  = current_temp_c + dt
        new_money_step = current_money_step + dm

    def _after():
        tc, ms = _apply_and_push_meters(new_temp_c, new_money_step)
        globals()['current_temp_c'] = tc; globals()['current_money_step'] = ms
    threading.Thread(target=_after, daemon=True).start()

    # Hardware pre hooks just before the video launches
    HW.reset_post_guard()
    HW.pre_for_state(state_id)

    print(f"[STATE] -> {state_id}", flush=True)
    if state_id == "0":
        _play_intro_loop()
    else:
        _play_video(s["video"])

    current_state = state_id
    _set_phase("playing")

def on_video_end():
    global _last_video_was_skipped
    if current_state == "0": return
    if phase != "playing": return
    _last_video_was_skipped = False   # natural end
    _show_choice_labels()

# =======================================================
# ===== SKIP logic – CLEAN JUMP TO FINAL FRAME (robust)
# =======================================================
def _wait_for_length(timeout_s=2.0):
    t0 = time.time()
    L = player.get_length() or 0
    while L <= 0 and (time.time() - t0) < timeout_s:
        time.sleep(0.02)
        L = player.get_length() or 0
    return max(0, L)

def _approx_frame_ms():
    with contextlib.suppress(Exception):
        fps = float(player.video_get_fps() or 0.0)
        if fps and fps > 1.0:
            return int(min(100, max(10, round(1000.0 / fps))))
    return 33

def _set_blackout(enable: bool):
    """Temporarily black out video so any tail maneuver is invisible."""
    try:
        player.video_set_adjust_int(vlc.VideoAdjustOption.Enable, 1 if enable else 0)
        player.video_set_adjust_float(vlc.VideoAdjustOption.Brightness, 0.0 if enable else 1.0)
    except Exception:
        pass

def _force_jump_to_tail():
    """
    Aggressive, verified jump near the tail that works even on builds
    where a single seek is ignored. Keeps playback visible-black.
    """
    _set_blackout(True)
    try:
        with contextlib.suppress(Exception): player.set_rate(1.0)
        with contextlib.suppress(Exception): player.set_pause(False)

        L = _wait_for_length(1.0)
        frame_ms = _approx_frame_ms()
        safety_ms = max(120, 3 * frame_ms)
        targets_ms = [max(0, L - s) for s in (safety_ms, safety_ms + 80, safety_ms + 160)]
        pos_targets = [0.98, 0.99, 0.995, 0.998, 0.999, 0.9995]

        orig_t = player.get_time() or 0

        def _moved():
            cur = player.get_time() or 0
            return cur > orig_t + max(40, frame_ms)

        for t in targets_ms:
            with contextlib.suppress(Exception): player.set_time(t)
            time.sleep(0.06)
            if _moved():
                break
        else:
            for p in pos_targets:
                with contextlib.suppress(Exception): player.set_position(p)
                time.sleep(0.06)
                if _moved():
                    break
            else:
                with contextlib.suppress(Exception): player.set_rate(16.0)
                deadline = time.time() + 0.25
                while time.time() < deadline and not _moved():
                    time.sleep(0.01)
                with contextlib.suppress(Exception): player.set_rate(1.0)
                if L > 0:
                    with contextlib.suppress(Exception): player.set_time(max(0, L - safety_ms))
                else:
                    with contextlib.suppress(Exception): player.set_position(0.999)
                time.sleep(0.06)

        with contextlib.suppress(Exception): player.set_pause(True)
        time.sleep(0.01)

        last_t = player.get_time() or 0
        for _ in range(16):
            with contextlib.suppress(Exception): player.next_frame()
            time.sleep(0.012)
            cur_t = player.get_time() or 0
            if cur_t <= last_t:
                break
            last_t = cur_t

        with contextlib.suppress(Exception): player.set_rate(1.0)

    finally:
        _set_blackout(False)

def _skip_to_last_frame_and_choice():
    if current_state == "0":
        return

    _detach_end_evt()

    try:
        _force_jump_to_tail()
    except Exception as e:
        log("skip_to_last_frame error:", e)

    _show_choice_labels()

def skip_current():
    global _last_video_was_skipped
    if current_state == "0":
        return
    if phase == "playing":
        _last_video_was_skipped = True  # will drive post-action rules (e.g., 6_1_1)
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
        enter_state(STATES["0"]["transitions"]["L"]); return
    if phase == "playing":
        skip_current(); return
    bump_inactivity()
    s = STATES[current_state]
    label = (s.get("end_words") or ("","",""))[0]
    if not label: return
    nxt = s.get("transitions", {}).get("L")
    if not nxt: return
    if nxt == "RESTART": _restart_loop_0()
    else: enter_state(nxt)

def handle_center():
    global phase
    if phase != "awaiting_choice": return
    bump_inactivity()
    s = STATES[current_state]
    label = (s.get("end_words") or ("","",""))[1]
    if not label: return
    nxt = s.get("transitions", {}).get("C")
    if not nxt: return
    if nxt == "RESTART": _restart_loop_0()
    else: enter_state(nxt)

def handle_right():
    global phase
    if phase == "playing": return
    bump_inactivity()
    s = STATES.get(current_state, {})
    label = (s.get("end_words") or ("","",""))[2]
    if label == "Restart": _restart_loop_0()

btn_left.when_pressed   = handle_left
btn_center.when_pressed = handle_center
btn_right.when_pressed  = handle_right

# ========================
# ====== Lifecycle =======
# ========================
CHROME_PROC = None
_shutting_down = False

def _quit(*_):
    global _shutting_down
    if _shutting_down: return
    _shutting_down = True

    # Hardware cleanup first
    with contextlib.suppress(Exception): HW.shutdown()

    _stop_intro_loop()
    _detach_end_evt()

    with contextlib.suppress(Exception): player.stop()
    with contextlib.suppress(Exception): player.set_media(None)
    with contextlib.suppress(Exception): player.release()
    with contextlib.suppress(Exception): inst.release()

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

    with contextlib.suppress(Exception): QTimer.singleShot(0, app.quit)
    threading.Timer(0.6, lambda: os._exit(0)).start()

signal.signal(signal.SIGINT, _quit)
signal.signal(signal.SIGTERM, _quit)
atexit.register(_quit)

# Keep event loop alive
tick = QTimer(); tick.timeout.connect(lambda: None); tick.start(100)
app.aboutToQuit.connect(_quit)

# Launch HUD window (state 0 sprite + words) and loop video 0
def launch_hud_initial():
    global CHROME_PROC
    COMMON_FLAGS = [
        "--incognito",
        f"--window-position={SMALL_RECT.x()},{SMALL_RECT.y()}",
        f"--window-size={SMALL_RECT.width()},{SMALL_RECT.height()}",
        "--no-first-run", "--no-default-browser-check",
        "--noerrdialogs", "--disable-session-crashed-bubble",
        "--overscroll-history-navigation=0", "--force-device-scale-factor=1",
        "--hide-scrollbars",
        f"--remote-debugging-port={REMOTE_PORT}",
        f"--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--enable-features=UseOzonePlatform", "--ozone-platform=x11",
    ]
    initial = SPRITES["base"]
    url = build_url(initial, words=("", "", ""))
    CHROME_PROC = subprocess.Popen(
        [CHROMIUM, *COMMON_FLAGS, f"--app={url}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    # place window
    t0 = time.time()
    while time.time() - t0 < 5.0:
        try:
            page_cdp, tgt = _connect_page_cdp(2.0); page_cdp.close()
            b = _connect_browser_cdp(2.0)
            win = b.get_window_for_target(tgt["id"])["result"]["windowId"]
            b.set_window_bounds(win, SMALL_RECT.x(), SMALL_RECT.y(), SMALL_RECT.width(), SMALL_RECT.height(), state="normal")
            b.close(); break
        except Exception:
            time.sleep(0.2)

launch_hud_initial()
enter_state("0")  # Start baseline + loop 0.mkv (fresh MediaListPlayer)

sys.exit(app.exec_())
