#!/usr/bin/env python3
"""
WatchDogsGo Linux WiFi + BLE Bridge
Emulates an ESP32 projectZero device over a virtual serial port,
using the host's WiFi adapter for scanning and built-in BT for BLE.

Usage:
    sudo python3 wdg_wifi_bridge.py --iface wlan1 --bt-iface hci1 --pty /tmp/esp32-pty

Then launch game with:
    sudo ./run.sh /tmp/esp32-pty
"""

import argparse
import asyncio
import logging
import os
import re
import subprocess
import sys
import time
import threading
import pty
import tty
import termios

try:
    from bleak import BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False

log = logging.getLogger("wdg_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

FIRMWARE_VERSION = "1.0.0"
BOOT_BANNER = f"WatchDogsGo version: v{FIRMWARE_VERSION}\r\n"

# Auth type mapping from iw scan output
def _auth_from_iw(security: str) -> str:
    s = security.upper()
    if "WPA3" in s:
        return "WPA3"
    if "WPA2" in s and "WPA " in s:
        return "WPA/WPA2"
    if "WPA2" in s:
        return "WPA2"
    if "WPA" in s:
        return "WPA"
    if "WEP" in s:
        return "WEP"
    return "OPEN"

# Band from frequency
def _band_from_freq(freq_mhz: int) -> str:
    if freq_mhz >= 5925:
        return "6GHz"
    if freq_mhz >= 5000:
        return "5GHz"
    return "2.4GHz"

# OUI vendor lookup (minimal, offline)
_OUI = {
    "00:50:F2": "Microsoft",
    "00:0C:E7": "Apple",
    "3C:5A:B4": "Google",
    "74:FE:CE": "Netgear",
    "C8:3A:35": "Tenda",
    "00:1A:2B": "Cisco",
}

def _vendor_from_bssid(bssid: str) -> str:
    prefix = bssid.upper()[:8]
    return _OUI.get(prefix, "")

def scan_wifi(iface: str) -> list[dict]:
    """Run iw scan on iface and return list of network dicts."""
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "scan"],
            capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        log.warning("iw scan timed out")
        return []
    except Exception as e:
        log.error("iw scan failed: %s", e)
        return []

    networks = []
    current = {}

    for line in result.stdout.splitlines():
        line = line.strip()

        # New BSS block
        m = re.match(r"BSS ([0-9a-f:]{17})", line, re.IGNORECASE)
        if m:
            if current.get("bssid"):
                networks.append(current)
            current = {"bssid": m.group(1).upper(), "ssid": "", "channel": "0",
                       "rssi": "-80", "security": "", "freq": 2412}
            continue

        if not current:
            continue

        m = re.match(r"SSID: (.+)", line)
        if m:
            current["ssid"] = m.group(1).strip()
            continue

        m = re.match(r"freq: (\d+)", line)
        if m:
            current["freq"] = int(m.group(1))
            continue

        m = re.match(r"DS Parameter set: channel (\d+)", line)
        if m:
            current["channel"] = m.group(1)
            continue

        m = re.match(r"\* primary channel: (\d+)", line)
        if m:
            current["channel"] = m.group(1)
            continue

        m = re.match(r"signal: ([-\d.]+) dBm", line)
        if m:
            current["rssi"] = str(int(float(m.group(1))))
            continue

        if "WPA" in line or "RSN" in line or "WEP" in line:
            current["security"] += " " + line.strip()

    if current.get("bssid"):
        networks.append(current)

    return networks


def format_network_csv(index: int, net: dict) -> str:
    """Format a network dict as the CSV line WatchDogsGo expects."""
    ssid    = net.get("ssid", "") or ""
    bssid   = net.get("bssid", "")
    channel = net.get("channel", "0")
    rssi    = net.get("rssi", "-80")
    auth    = _auth_from_iw(net.get("security", ""))
    band    = _band_from_freq(net.get("freq", 2412))
    vendor  = _vendor_from_bssid(bssid)
    return f'"{index}","{ssid}","{vendor}","{bssid}","{channel}","{auth}","{rssi}","{band}"\r\n'


async def _ble_scan_async(bt_iface: str, duration: float = 8.0) -> list[dict]:
    """Async BLE scan using bleak 3.x API."""
    devices = []
    try:
        results = await BleakScanner.discover(
            timeout=duration,
            adapter=bt_iface,
            return_adv=True,
        )
        for addr, (device, adv_data) in results.items():
            rssi = adv_data.rssi if adv_data.rssi is not None else -99
            name = device.name or adv_data.local_name or ""
            devices.append({
                "mac": addr,
                "rssi": rssi,
                "name": name,
            })
    except Exception as e:
        log.warning("BLE scan error: %s", e)
    return devices


def scan_ble(bt_iface: str, duration: float = 8.0) -> list[dict]:
    """Run BLE scan and return list of device dicts."""
    if not BLEAK_AVAILABLE:
        log.warning("bleak not installed — BLE scanning disabled")
        return []
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_ble_scan_async(bt_iface, duration))
        loop.close()
        return result
    except Exception as e:
        log.error("BLE scan failed: %s", e)
        return []


def format_ble_line(index: int, device: dict) -> str:
    """Format a BLE device as the line WatchDogsGo expects.
    Format: '1. AA:BB:CC:DD:EE:FF RSSI: -65 dBm Name: DeviceName'
    """
    mac  = device.get("mac", "00:00:00:00:00:00")
    rssi = device.get("rssi", -99)
    name = device.get("name", "")
    if name:
        return f"{index}. {mac} RSSI: {rssi} dBm Name: {name}\r\n"
    return f"{index}. {mac} RSSI: {rssi} dBm\r\n"


def set_monitor_mode(iface: str) -> bool:
    """Put interface into monitor mode. Returns True on success."""
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=True)
        subprocess.run(["iw", "dev", iface, "set", "type", "monitor"], check=True)
        subprocess.run(["ip", "link", "set", iface, "up"], check=True)
        log.info("Set %s to monitor mode", iface)
        return True
    except subprocess.CalledProcessError as e:
        log.error("Failed to set monitor mode: %s", e)
        return False


def restore_managed_mode(iface: str):
    """Restore interface to managed mode."""
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=False)
        subprocess.run(["iw", "dev", iface, "set", "type", "managed"], check=False)
        subprocess.run(["ip", "link", "set", iface, "up"], check=False)
        log.info("Restored %s to managed mode", iface)
    except Exception as e:
        log.warning("Could not restore managed mode: %s", e)


class WifiBridge:
    def __init__(self, iface: str, pty_path: str, bt_iface: str = "hci1"):
        self.iface = iface
        self.pty_path = pty_path
        self.bt_iface = bt_iface
        self._master_fd = None
        self._slave_fd = None
        self._running = False
        self._scan_requested = False
        self._lock = threading.Lock()

    def start(self):
        # Create PTY
        self._master_fd, self._slave_fd = pty.openpty()
        slave_name = os.ttyname(self._slave_fd)

        # Symlink to requested path
        if os.path.exists(self.pty_path) or os.path.islink(self.pty_path):
            os.unlink(self.pty_path)
        os.symlink(slave_name, self.pty_path)
        log.info("PTY: %s -> %s", self.pty_path, slave_name)

        # Set raw mode
        tty.setraw(self._master_fd)

        self._running = True

        # Send boot banner so game detects firmware version
        self._write(BOOT_BANNER)

        # Start reader thread
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()

        log.info("Bridge running. Waiting for game commands on %s", self.pty_path)

        try:
            while self._running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        self._running = False
        if os.path.islink(self.pty_path):
            os.unlink(self.pty_path)
        if self._master_fd:
            os.close(self._master_fd)

    def _write(self, data: str):
        try:
            os.write(self._master_fd, data.encode())
        except OSError:
            pass

    def _read_loop(self):
        buf = b""
        while self._running:
            try:
                chunk = os.read(self._master_fd, 256)
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode("utf-8", errors="replace").strip()
                    if cmd:
                        self._handle_command(cmd)
            except OSError:
                break
            except Exception as e:
                log.debug("Read error: %s", e)

    def _handle_command(self, cmd: str):
        log.info("CMD: %s", cmd)

        if cmd == "scan_networks":
            threading.Thread(target=self._do_scan, daemon=True).start()

        elif cmd == "ping":
            self._write(f"pong v{FIRMWARE_VERSION}\r\n")

        elif cmd == "stop":
            self._write("all stopped\r\n")

        elif cmd == "scan_bt":
            threading.Thread(target=self._do_ble_scan, daemon=True).start()

        else:
            # Echo unknown commands so game doesn't hang
            log.debug("Unknown command: %s", cmd)

    def _do_scan(self):
        log.info("Scanning on %s...", self.iface)
        networks = scan_wifi(self.iface)
        log.info("Found %d networks", len(networks))

        for i, net in enumerate(networks):
            line = format_network_csv(i, net)
            self._write(line)
            time.sleep(0.01)  # small delay to avoid flooding serial buffer

        self._write("scan results printed\r\n")

    def _do_ble_scan(self):
        log.info("BLE scanning on %s...", self.bt_iface)
        devices = scan_ble(self.bt_iface, duration=8.0)
        log.info("Found %d BLE devices", len(devices))

        for i, device in enumerate(devices):
            line = format_ble_line(i + 1, device)
            self._write(line)
            time.sleep(0.01)

        self._write("BLE scan done\r\n")


def main():
    parser = argparse.ArgumentParser(description="WatchDogsGo Linux WiFi + BLE Bridge")
    parser.add_argument("--iface", default="wlan1",
                        help="WiFi interface to use for scanning (default: wlan1)")
    parser.add_argument("--bt-iface", default="hci1",
                        help="Bluetooth interface for BLE scanning (default: hci1)")
    parser.add_argument("--pty", default="/tmp/esp32-pty",
                        help="PTY symlink path for WatchDogsGo (default: /tmp/esp32-pty)")
    parser.add_argument("--no-monitor", action="store_true",
                        help="Skip setting monitor mode (use if already in monitor mode)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: Must run as root (sudo)", file=sys.stderr)
        sys.exit(1)

    if not BLEAK_AVAILABLE:
        log.warning("bleak not installed — BLE scanning disabled. Install with: pip install bleak --break-system-packages")

    if not args.no_monitor:
        if not set_monitor_mode(args.iface):
            print(f"ERROR: Could not set {args.iface} to monitor mode", file=sys.stderr)
            sys.exit(1)

    bridge = WifiBridge(args.iface, args.pty, args.bt_iface)
    try:
        bridge.start()
    finally:
        if not args.no_monitor:
            restore_managed_mode(args.iface)


if __name__ == "__main__":
    main()
