# Pi 5: Video on MAIN display (covers taskbar), solid color + short text on secondary.
# GPIO13 starts playback. On video end: return to black on MAIN, and update
# the SECONDARY window with a new random color and short random text.
# Requires: python-vlc, PyQt5, gpiozero

import os, sys, signal, re, subprocess, random, vlc
from gpiozero import Button
from PyQt5.QtWidgets import QApplication, QWidget, QLabel
from PyQt5.QtCore import Qt, QTimer, QRect

VIDEO = "/home/life-forever/Desktop/Life-Forever/output.mkv"
BTN   = 13
MAIN  = "HDMI-A-2"   # video
RED   = "HDMI-A-1"   # color/text panel (800x480)

# Ensure X11 embedding
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

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

app = QApplication(sys.argv)
mon = get_monitor_geos()
if MAIN not in mon or RED not in mon:
    print("Couldn't find monitors. Found:", list(mon.keys()))
    sys.exit(1)

def make_window(rect: QRect, color_css: str, bypass_wm: bool):
    flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    if bypass_wm:
        flags |= Qt.X11BypassWindowManagerHint  # covers panels/taskbar
    w = QWidget(None, flags)
    w.setAttribute(Qt.WA_NativeWindow, True)
    w.setGeometry(rect)
    w.move(rect.topLeft())
    w.resize(rect.size())
    w.setStyleSheet(f"background-color:{color_css};")
    w.show()
    w.raise_()
    return w

# --- Secondary window: color + centered label ---
red_w  = make_window(mon[RED], "#c00", bypass_wm=False)
label  = QLabel(red_w)
label.setAlignment(Qt.AlignCenter)
label.setStyleSheet("color: white; font-size: 36px; font-weight: 600;")
label.setGeometry(red_w.rect())
label.show()          # <-- ensure the label is visible
label.raise_()        # <-- ensure it's above the background

def set_red_panel(color_css: str, text: str):
    red_w.setStyleSheet(f"background-color:{color_css};")
    label.setGeometry(red_w.rect())
    label.setText(text)
    label.raise_()
    red_w.update()

# --- Main window: black underlay for video (unmanaged so it covers taskbar) ---
main_w = make_window(mon[MAIN], "#000", bypass_wm=True)
main_xid = int(main_w.winId())

# --- libVLC → render into main window ---
inst = vlc.Instance("--avcodec-hw=drm", "--no-video-title-show")
player = inst.media_player_new()
player.set_xwindow(main_xid)

# --- Button + play/stop state ---
btn = Button(BTN, pull_up=True, bounce_time=0.3)
_playing = False

def play_once():
    global _playing
    if _playing:
        return
    _playing = True
    m = inst.media_new(VIDEO)
    player.set_media(m)
    player.play()

def _on_stop(*_):
    # Return main to black and update the secondary panel with random color/text
    global _playing
    _playing = False
    main_w.setStyleSheet("background-color:#000;")
    main_w.update()

    texts = ["hello", "ready", "again", "press", "ok", "go", "done", "next"]
    # Bright-ish random color
    def rand_hex():
        return "#{:02x}{:02x}{:02x}".format(
            random.randint(128, 255),
            random.randint(32, 128),
            random.randint(32, 128)
        )
    set_red_panel(rand_hex(), random.choice(texts))

em = player.event_manager()
em.event_attach(vlc.EventType.MediaPlayerEndReached, _on_stop)
em.event_attach(vlc.EventType.MediaPlayerEncounteredError, _on_stop)
em.event_attach(vlc.EventType.MediaPlayerStopped, _on_stop)

btn.when_pressed = play_once

# --- Clean exit (Ctrl-C/SIGTERM) while Qt runs ---
def _quit(*_):
    try:
        player.stop()
        player.release()
        inst.release()
        btn.close()
    except Exception:
        pass
    app.quit()

signal.signal(signal.SIGINT, _quit)
signal.signal(signal.SIGTERM, _quit)

# Keep the event loop active (and GPIO callbacks pumping)
tick = QTimer()
tick.timeout.connect(lambda: None)
tick.start(100)

app.aboutToQuit.connect(_quit)

# Initialize secondary panel once
set_red_panel("#c00", "ready")

sys.exit(app.exec_())
