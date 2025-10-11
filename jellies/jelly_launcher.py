#!/usr/bin/env python3
"""
Jelly Launcher (no autostart): shows a default sprite immediately,
then plays a random animation when the button is pressed. After the
random animation ends, the default sprite remains visible.

- Place your *.html files in /home/pi/jelly (configurable via HTML_DIR).
- Set DEFAULT_FILE to the filename of your default sprite (e.g. "base_jelly.html").
- Wire a button between GPIO 23 and GND (change BUTTON_GPIO if needed).

Tip: Keep the default Chromium window open in kiosk mode. When a button
press occurs, a *second* Chromium window opens on top with the random
animation, then closes after PLAY_SECONDS, revealing the default again.
"""

import os
import random
import signal
import subprocess
import threading
import time
from pathlib import Path

# --- Configuration ---
HTML_DIR = Path.home() / "jelly"   # e.g. /home/pi/jelly
DEFAULT_FILE = "base_jelly.html"   # file to show at startup (must exist in HTML_DIR)
PLAY_SECONDS = 8                   # time to show the random animation
BUTTON_GPIO = 23                   # BCM numbering
DEBOUNCE_MS = 250
CHROMIUM = "chromium-browser"      # use "chromium" on newer Raspberry Pi OS if needed

# Chromium flags for kiosk 800x480
COMMON_FLAGS = [
    "--kiosk",
    "--incognito",
    "--app=",
    "--window-size=800,480",
    "--start-fullscreen",
    "--autoplay-policy=no-user-gesture-required",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--overscroll-history-navigation=0",
    "--force-device-scale-factor=1",
]

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception as e:
    GPIO = None
    print("[WARN] RPi.GPIO not available; running in 'demo' mode without GPIO.")
    print("       Error:", e)

default_proc = None     # persistent Chromium showing default
overlay_proc = None     # transient Chromium for random animation
busy_lock = threading.Lock()
last_press_ts = 0

def html_pool():
    if not HTML_DIR.exists():
        HTML_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(HTML_DIR.glob("*.html"))

def find_default_file():
    # Exact match first
    candidate = HTML_DIR / DEFAULT_FILE
    if candidate.exists():
        return candidate
    # Fallback: first file starting with "base_"
    for f in html_pool():
        if f.name.startswith("base_"):
            return f
    # Fallback: any html file
    files = html_pool()
    return files[0] if files else None

def spawn_chromium(url: str):
    flags = COMMON_FLAGS.copy()
    flags[flags.index("--app=")] = f"--app={url}"
    return subprocess.Popen(
        [CHROMIUM, *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,  # create new process group
    )

def launch_default_once():
    global default_proc
    df = find_default_file()
    if not df:
        print(f"[ERROR] No HTML files found in {HTML_DIR}.")
        return
    url = f"file://{df}"
    print(f"[INFO] Launching DEFAULT: {df.name}")
    default_proc = spawn_chromium(url)

def kill_proc_tree(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.2)
    except Exception as e:
        print("[WARN] Failed to kill process:", e)

def play_random_overlay():
    global overlay_proc
    files = html_pool()
    if not files:
        print(f"[WARN] No HTML files in {HTML_DIR}")
        return

    # Avoid picking the exact default file if present (optional)
    df = find_default_file()
    candidates = [f for f in files if df is None or f.resolve() != df.resolve()]
    if not candidates:
        candidates = files  # if default is the only file, allow it

    choice = random.choice(candidates)
    url = f"file://{choice}"
    print(f"[INFO] Overlay START: {choice.name}")
    overlay_proc = spawn_chromium(url)
    try:
        time.sleep(PLAY_SECONDS)
    finally:
        print("[INFO] Overlay END")
        kill_proc_tree(overlay_proc)
        overlay_proc = None

def on_button_press(channel=None):
    global last_press_ts
    now = time.time()
    if (now - last_press_ts) * 1000 < DEBOUNCE_MS:
        return
    last_press_ts = now

    if not busy_lock.acquire(blocking=False):
        print("[INFO] Ignored press: overlay already running.")
        return

    def worker():
        try:
            play_random_overlay()
        finally:
            busy_lock.release()

    threading.Thread(target=worker, daemon=True).start()

def setup_gpio():
    if GPIO is None:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(BUTTON_GPIO, GPIO.FALLING, callback=on_button_press, bouncetime=DEBOUNCE_MS)

def main():
    setup_gpio()
    launch_default_once()
    print("[READY] Default is showing. Press the button to play a random overlay.")
    try:
        while True:
            if GPIO is None:
                # Demo mode: simulate a press every 10s
                on_button_press()
                time.sleep(10)
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        # Only kill the overlay; keep default open unless exiting
        if overlay_proc is not None:
            kill_proc_tree(overlay_proc)
        if default_proc is not None:
            kill_proc_tree(default_proc)
        if GPIO is not None:
            GPIO.cleanup()

if __name__ == "__main__":
    main()
