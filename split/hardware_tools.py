import os, time, threading, contextlib

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

# OSC (simple 1 / 0 client)
try:
    from pythonosc.udp_client import SimpleUDPClient
except Exception:
    SimpleUDPClient = None

# ======= Hardware IO Config =======
SERIAL_PORT  = os.environ.get("ARDUINO_PORT") or None  # None = auto-select
SERIAL_BAUD  = int(os.environ.get("ARDUINO_BAUD", "9600"))
SERIAL_INTER = float(os.environ.get("ARDUINO_INTER", "0.05"))  # gap between multi-char sends

# Miner credentials
MINER1_IP   = os.environ.get("MINER1_IP",   "10.162.142.177")
MINER1_USER = os.environ.get("MINER1_USER", "root")
MINER1_PW   = os.environ.get("MINER1_PW",   "lifeforever")
MINER2_IP   = os.environ.get("MINER2_IP",   "10.162.142.113")
MINER2_USER = os.environ.get("MINER2_USER", "root")
MINER2_PW   = os.environ.get("MINER2_PW",   "lifeforever")

# OSC config
OSC_IP   = os.environ.get("KARAOKE_OSC_IP", "127.0.0.1")
OSC_PORT = int(os.environ.get("KARAOKE_OSC_PORT", "8000"))

if SimpleUDPClient is not None:
    try:
        _osc_client = SimpleUDPClient(OSC_IP, OSC_PORT)
        print(f"[OSC] Ready on {OSC_IP}:{OSC_PORT}", flush=True)
    except Exception as _e:
        print(f"[WARN] OSC init failed: {_e}", flush=True)
        _osc_client = None
else:
    print("[WARN] python-osc not installed; OSC disabled.", flush=True)
    _osc_client = None

def osc_karaoke_on():
    """Send /karaoke 1."""
    if not _osc_client:
        return
    try:
        _osc_client.send_message("/karaoke", 1)
        print("[OSC] /karaoke 1", flush=True)
    except Exception as e:
        print(f"[WARN] OSC send failed: {e}", flush=True)

def osc_karaoke_off():
    """Send /karaoke 0."""
    if not _osc_client:
        return
    try:
        _osc_client.send_message("/karaoke", 0)
        print("[OSC] /karaoke 0", flush=True)
    except Exception as e:
        print(f"[WARN] OSC send failed: {e}", flush=True)

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
    - pre_for_state() runs immediately before a video starts (or loop 0.mkv starts)
    - post_for_state() runs once after a video *finishes or is skipped* (with skip-awareness)
    """
    def __init__(self):
        self.serial = SerialBridge(SERIAL_PORT, SERIAL_BAUD, inter=SERIAL_INTER)
        self.miners = MinerController(
            MINER1_IP, MINER1_USER, MINER1_PW,
            MINER2_IP, MINER2_USER, MINER2_PW
        )
        self._last_post_done_state = None

    def pre_for_state(self, sid: str):
        # (1) BLANK SLATE before every 0.mkv loop (boot and every Restart):
        #     - Stop miners
        #     - Send f, x, c to Arduino
        if sid == "0":
            self.miners.stop_both()  # best-effort; non-blocking
            self.serial.send_seq(["f", "x", "c"])

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

        # (10) f and x after 6_1_1 finishes OR is skipped
        elif sid == "6_1_1":
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
