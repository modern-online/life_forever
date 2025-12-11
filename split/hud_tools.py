import os, sys, json, time, shutil, subprocess, urllib.request, urllib.parse, threading, contextlib
from pathlib import Path

from PyQt5.QtCore import QRect
import websocket

BASE_DIR = Path(__file__).resolve().parent
HTML_DIR = BASE_DIR / "jellies"
DEFAULT_FILE = "base_jelly.html"

# Chromium & CDP
REMOTE_PORT  = 9222
PROFILE_DIR  = HTML_DIR / ".jelly_profile"
PROFILE_DIR.mkdir(exist_ok=True)

CHROMIUM = os.environ.get("CHROMIUM") or os.environ.get("CHROMIUM_BIN")
for c in (CHROMIUM, "chromium", "chromium-browser", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
    if c and (shutil.which(c) or Path(c).exists()):
        CHROMIUM = c
        break
if not CHROMIUM:
    print("Chromium not found. Set CHROMIUM=/path/to/chromium", file=sys.stderr)
    sys.exit(1)

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
        if t.get("type") in ("page", "app") and str(t.get("url", "")).startswith("file://"):
            return t
    for t in ts:
        if t.get("type") in ("page", "app"):
            return t
    return None

class CDPPage:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=3.0)
        self.i = 0
    def _send(self, method, params=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self.i:
                return resp
    def enable_page(self): self._send("Page.enable")
    def nav(self, url):
        self.enable_page()
        self._send("Page.navigate", {"url": url})
    def front(self): self._send("Page.bringToFront")
    def enable_runtime(self): self._send("Runtime.enable")
    def eval_js(self, expression, await_promise=True):
        self.enable_runtime()
        return self._send("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
    def close(self):
        with contextlib.suppress(Exception): self.ws.close()

class CDPBrowser:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=3.0)
        self.i = 0
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
        params = {
            "windowId": windowId,
            "bounds": {"left": left, "top": top, "width": width, "height": height, "windowState": state},
        }
        return self._send("Browser.setWindowBounds", params)
    def close(self):
        with contextlib.suppress(Exception): self.ws.close()

def _connect_page_cdp(wait=10.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < wait:
        try:
            tgt = _pick_target()
            if tgt and "webSocketDebuggerUrl" in tgt:
                return CDPPage(tgt["webSocketDebuggerUrl"]), tgt
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"CDP(page) connect failed: {last}")

def _cdp_eval_js(js_code, tries=30, sleep_s=0.1):
    last_err = None
    for _ in range(tries):
        try:
            page_cdp, _ = _connect_page_cdp(5.0)
            try:
                page_cdp.eval_js(js_code)
                return True
            finally:
                page_cdp.close()
        except Exception as e:
            last_err = e
            time.sleep(sleep_s)
    print("CDP eval failed:", last_err, file=sys.stderr)
    return False

def build_url(p: Path, words=("","","")) -> str:
    params = {"w1": words[0] or "", "w2": words[1] or "", "w3": words[2] or ""}
    qs = urllib.parse.urlencode(params, doseq=False, safe="")
    return f"file://{p}?{qs}"

def hud_set_words(words):
    esc = lambda s: (s or "").replace("\\", "\\\\").replace("'", "\\'")
    js = f"window.JellyHUD && JellyHUD.setWords(['{esc(words[0])}','{esc(words[1])}','{esc(words[2])}']);"
    _cdp_eval_js(js)

def hud_set_words_async(words):
    threading.Thread(target=lambda: hud_set_words(words), daemon=True).start()

def hud_ready():
    return _cdp_eval_js("typeof window.JellyHUD !== 'undefined' ? true : (function(){return true;})();")

def hud_set_meters_percent(temp_pct: int, money_pct: int):
    js = f"window.JellyHUD && JellyHUD.set({int(temp_pct)}, {int(money_pct)});"
    _cdp_eval_js(js)

def navigate_hud_to(sprite_path: Path, initial_words=None):
    if initial_words is None:
        initial_words = ["", "", ""]
    def worker():
        try:
            page_cdp, _tgt = _connect_page_cdp(5.0)
            try:
                url = build_url(sprite_path, words=("", "", ""))
                url += ("&" if "?" in url else "?") + f"t={int(time.time()*1000)}"
                page_cdp.nav(url)
                page_cdp.front()
            finally:
                page_cdp.close()
            hud_ready()
            hud_set_words(initial_words)
        except Exception as e:
            print("navigate_hud_to error:", e)
    threading.Thread(target=worker, daemon=True).start()

def launch_hud_initial(initial_sprite: Path, small_rect: QRect):
    COMMON_FLAGS = [
        "--incognito",
        f"--window-position={small_rect.x()},{small_rect.y()}",
        f"--window-size={small_rect.width()},{small_rect.height()}",
        "--no-first-run", "--no-default-browser-check",
        "--noerrdialogs", "--disable-session-crashed-bubble",
        "--overscroll-history-navigation=0", "--force-device-scale-factor=1",
        "--hide-scrollbars",
        f"--remote-debugging-port={REMOTE_PORT}",
        f"--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--enable-features=UseOzonePlatform", "--ozone-platform=x11",
    ]
    url = build_url(initial_sprite, words=("", "", ""))
    CHROME_PROC = subprocess.Popen(
        [CHROMIUM, *COMMON_FLAGS, f"--app={url}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # place window
    t0 = time.time()
    while time.time() - t0 < 5.0:
        try:
            page_cdp, tgt = _connect_page_cdp(2.0)
            page_cdp.close()
            ver = _json_version()
            b = CDPBrowser(ver["webSocketDebuggerUrl"])
            win = b.get_window_for_target(tgt["id"])["result"]["windowId"]
            b.set_window_bounds(win, small_rect.x(), small_rect.y(),
                                small_rect.width(), small_rect.height(),
                                state="normal")
            b.close()
            break
        except Exception:
            time.sleep(0.2)
    return CHROME_PROC
