#!/usr/bin/env python3
"""
WatchDogsGo Linux WiFi + BLE Bridge
Emulates an ESP32 projectZero device over a virtual serial port,
using the host's WiFi adapter for scanning and built-in BT for BLE.

Usage:
    sudo python3 wdg_wifi_bridge.py --iface wlan1 --bt-iface hci1 --sniffer-iface wlan2 --no-monitor --pty /tmp/esp32-pty

Then launch game with:
    sudo ./run.sh /tmp/esp32-pty

Changes from original (FusedStamen fork):
  - BLE fast-fail detection: times each scan and logs a warning if it completes
    in under 2 seconds with 0 results, indicating the Bluetooth adapter has dropped
  - Scan rate limiter: returns cached results if scan requested within SCAN_COOLDOWN
    seconds of last scan, preventing mt7921u timeout under rapid game requests
  - version command: returns firmware version string the game expects
  - Handshake/sniffer dispatch: stubs out to external scripts (hs_capture.py,
    pkt_sniff.py) rather than running inline — bridge stays stable, scripts
    handle their own hardware
  - Removed stale wlan1 restore from _stop_hs_capture (was leftover hcxdumptool
    comment, airodump-ng does not take down all interfaces)
  - --no-monitor is now the recommended default for uConsole stability

Dependencies: bleak, iw
Optional: hs_capture.py, pkt_sniff.py (for handshake/sniffer dispatch)
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

try:
    from bleak import BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False

log = logging.getLogger("wdg_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

FIRMWARE_VERSION = "1.0.0"
BOOT_BANNER = f"WatchDogsGo version: v{FIRMWARE_VERSION}\r\n"

# Minimum seconds between live iw scans — returns cache if called faster
SCAN_COOLDOWN = 5.0


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


def _band_from_freq(freq_mhz: int) -> str:
    if freq_mhz >= 5925:
        return "6GHz"
    if freq_mhz >= 5000:
        return "5GHz"
    return "2.4GHz"


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
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "scan"],
            capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        log.warning("iw scan timed out on %s", iface)
        return []
    except Exception as e:
        log.error("iw scan failed: %s", e)
        return []

    networks = []
    current = {}

    for line in result.stdout.splitlines():
        line = line.strip()
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
    ssid    = net.get("ssid", "") or ""
    bssid   = net.get("bssid", "")
    channel = net.get("channel", "0")
    rssi    = net.get("rssi", "-80")
    auth    = _auth_from_iw(net.get("security", ""))
    band    = _band_from_freq(net.get("freq", 2412))
    vendor  = _vendor_from_bssid(bssid)
    return f'"{index}","{ssid}","{vendor}","{bssid}","{channel}","{auth}","{rssi}","{band}"\r\n'


async def _ble_scan_async(bt_iface: str, duration: float = 8.0) -> list[dict]:
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
            devices.append({"mac": addr, "rssi": rssi, "name": name})
    except Exception as e:
        log.warning("BLE scan error: %s", e)
    return devices


def scan_ble(bt_iface: str, duration: float = 8.0) -> list[dict]:
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
    mac  = device.get("mac", "00:00:00:00:00:00")
    rssi = device.get("rssi", -99)
    name = device.get("name", "")
    if name:
        return f"{index}. {mac} RSSI: {rssi} dBm Name: {name}\r\n"
    return f"{index}. {mac} RSSI: {rssi} dBm\r\n"


def set_monitor_mode(iface: str) -> bool:
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
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=False)
        subprocess.run(["iw", "dev", iface, "set", "type", "managed"], check=False)
        subprocess.run(["ip", "link", "set", iface, "up"], check=False)
        log.info("Restored %s to managed mode", iface)
    except Exception as e:
        log.warning("Could not restore managed mode: %s", e)


class WifiBridge:
    def __init__(self, iface: str, pty_path: str, bt_iface: str = "hci1",
                 sniffer_iface: str = "wlan2", loot_dir: str = ""):
        self.iface = iface
        self.pty_path = pty_path
        self.bt_iface = bt_iface
        self.sniffer_iface = sniffer_iface
        self.loot_dir = loot_dir or os.path.expanduser("~/python/WatchDogsGo/loot")
        self._master_fd = None
        self._slave_fd = None
        self._running = False
        self._lock = threading.Lock()
        # Scan rate limiter
        self._last_scan_time = 0.0
        self._last_scan_results: list[dict] = []
        self._scan_in_progress = False

    def start(self):
        self._master_fd, self._slave_fd = pty.openpty()
        slave_name = os.ttyname(self._slave_fd)
        if os.path.exists(self.pty_path) or os.path.islink(self.pty_path):
            os.unlink(self.pty_path)
        os.symlink(slave_name, self.pty_path)
        log.info("PTY: %s -> %s", self.pty_path, slave_name)
        tty.setraw(self._master_fd)
        self._running = True
        self._write(BOOT_BANNER)
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
        elif cmd == "version":
            self._write(f"WatchDogsGo version: v{FIRMWARE_VERSION}\r\n")
        elif cmd == "stop":
            self._write("all stopped\r\n")
        elif cmd == "scan_bt":
            threading.Thread(target=self._do_ble_scan, daemon=True).start()
        elif cmd in ("start_pkt_sniff", "start_sniffer"):
            threading.Thread(target=self._dispatch_pkt_sniff, daemon=True).start()
        elif cmd in ("stop_pkt_sniff", "stop_sniffer", "sniffer_stop"):
            self._write("sniffer stop: not active\r\n")
        elif cmd in ("start_handshake", "start_handshake_serial"):
            threading.Thread(target=self._dispatch_hs, daemon=True).start()
        elif cmd == "stop_handshake":
            self._write("handshake stop: not active\r\n")
        else:
            log.debug("Unknown command: %s", cmd)

    # ------------------------------------------------------------------
    # WiFi scan with rate limiter + cache
    # ------------------------------------------------------------------

    def _do_scan(self):
        with self._lock:
            now = time.time()
            since_last = now - self._last_scan_time

            # If a scan is already running, wait for it rather than pile on
            if self._scan_in_progress:
                log.info("Scan already in progress — waiting")
                # Release lock and poll until done
            elif since_last < SCAN_COOLDOWN and self._last_scan_results:
                log.info("Returning cached results (%.1fs since last scan, cooldown=%.1fs)",
                         since_last, SCAN_COOLDOWN)
                self._send_results(self._last_scan_results, cached=True)
                return
            else:
                self._scan_in_progress = True

        # Wait if another scan was already running
        if not self._scan_in_progress:
            deadline = time.time() + 20.0
            while time.time() < deadline:
                time.sleep(0.2)
                with self._lock:
                    if not self._scan_in_progress:
                        self._send_results(self._last_scan_results, cached=True)
                        return

        try:
            log.info("Scanning on %s...", self.iface)
            networks = scan_wifi(self.iface)

            with self._lock:
                if networks:
                    self._last_scan_results = networks
                    self._last_scan_time = time.time()
                elif self._last_scan_results:
                    # Timeout — return cache rather than empty
                    log.warning("Scan returned 0 results — serving cache (%d networks)",
                                len(self._last_scan_results))
                    networks = self._last_scan_results

            log.info("Found %d networks", len(networks))
            self._send_results(networks)
        finally:
            with self._lock:
                self._scan_in_progress = False

    def _send_results(self, networks: list[dict], cached: bool = False) -> None:
        if cached:
            log.info("Sending %d cached networks", len(networks))
        for i, net in enumerate(networks):
            self._write(format_network_csv(i, net))
            time.sleep(0.01)
        self._write("scan results printed\r\n")

    # ------------------------------------------------------------------
    # BLE scan
    # ------------------------------------------------------------------

    def _do_ble_scan(self):
        log.info("BLE scanning on %s...", self.bt_iface)
        t_start = time.time()
        devices = scan_ble(self.bt_iface, duration=8.0)
        elapsed = time.time() - t_start
        if elapsed < 2.0 and len(devices) == 0:
            log.warning("BLE scan completed in %.1fs with 0 results — adapter may have dropped (hci=%s)",
                        elapsed, self.bt_iface)
            self._write(f"BLE adapter warning: scan returned in {elapsed:.1f}s with 0 devices\r\n")
        else:
            log.info("Found %d BLE devices in %.1fs", len(devices), elapsed)
        for i, device in enumerate(devices):
            self._write(format_ble_line(i + 1, device))
            time.sleep(0.01)
        self._write("BLE scan done\r\n")

    # ------------------------------------------------------------------
    # Handshake dispatch — calls external hs_capture.py if present
    # ------------------------------------------------------------------

    def _dispatch_hs(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hs_capture.py")
        if not os.path.exists(script):
            log.warning("hs_capture.py not found — handshake capture unavailable")
            self._write("handshake error: hs_capture.py not found. "
                        "Plug in AWUS036ACM or XIAO and ensure hs_capture.py is present.\r\n")
            return
        try:
            log.info("Dispatching handshake capture to %s", script)
            self._write("handshake capture starting...\r\n")
            result = subprocess.run(
                [sys.executable, script,
                 "--iface", self.sniffer_iface,
                 "--loot-dir", self.loot_dir],
                capture_output=True, text=True, timeout=300
            )
            for line in result.stdout.splitlines():
                self._write(line + "\r\n")
            if result.returncode != 0:
                log.warning("hs_capture.py exited with %d", result.returncode)
        except subprocess.TimeoutExpired:
            log.warning("hs_capture.py timed out")
            self._write("handshake capture timed out\r\n")
        except Exception as e:
            log.error("hs_capture dispatch error: %s", e)
            self._write(f"handshake error: {e}\r\n")

    # ------------------------------------------------------------------
    # Packet sniffer dispatch — calls external pkt_sniff.py if present
    # ------------------------------------------------------------------

    def _dispatch_pkt_sniff(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pkt_sniff.py")
        if not os.path.exists(script):
            log.warning("pkt_sniff.py not found — packet sniffer unavailable")
            self._write("sniffer error: pkt_sniff.py not found. "
                        "Plug in AWUS036ACM and ensure pkt_sniff.py is present.\r\n")
            return
        try:
            log.info("Dispatching packet sniffer to %s", script)
            self._write("sniffer starting...\r\n")
            result = subprocess.run(
                [sys.executable, script,
                 "--iface", self.sniffer_iface,
                 "--loot-dir", self.loot_dir],
                capture_output=True, text=True, timeout=300
            )
            for line in result.stdout.splitlines():
                self._write(line + "\r\n")
            if result.returncode != 0:
                log.warning("pkt_sniff.py exited with %d", result.returncode)
        except subprocess.TimeoutExpired:
            log.warning("pkt_sniff.py timed out")
            self._write("sniffer timed out\r\n")
        except Exception as e:
            log.error("pkt_sniff dispatch error: %s", e)
            self._write(f"sniffer error: {e}\r\n")

    @staticmethod
    def _check_tool(name: str) -> bool:
        import shutil
        return shutil.which(name) is not None


def main():
    parser = argparse.ArgumentParser(description="WatchDogsGo Linux WiFi + BLE Bridge")
    parser.add_argument("--iface", default="wlan1",
                        help="WiFi interface for scanning (default: wlan1)")
    parser.add_argument("--bt-iface", default="hci1",
                        help="Bluetooth interface for BLE scanning (default: hci1)")
    parser.add_argument("--sniffer-iface", default="wlan2",
                        help="WiFi interface for handshake/sniffer dispatch (default: wlan2)")
    parser.add_argument("--pty", default="/tmp/esp32-pty",
                        help="PTY symlink path for WatchDogsGo (default: /tmp/esp32-pty)")
    parser.add_argument("--no-monitor", action="store_true",
                        help="Skip setting monitor mode on --iface (recommended for uConsole)")
    parser.add_argument("--loot-dir", default="",
                        help="Directory to save captures (default: ~/python/WatchDogsGo/loot)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ERROR: Must run as root (sudo)", file=sys.stderr)
        sys.exit(1)

    if not BLEAK_AVAILABLE:
        log.warning("bleak not installed — BLE scanning disabled. "
                    "Install with: pip install bleak --break-system-packages")

    if not args.no_monitor:
        if not set_monitor_mode(args.iface):
            print(f"ERROR: Could not set {args.iface} to monitor mode", file=sys.stderr)
            sys.exit(1)

    bridge = WifiBridge(
        iface=args.iface,
        pty_path=args.pty,
        bt_iface=args.bt_iface,
        sniffer_iface=args.sniffer_iface,
        loot_dir=args.loot_dir,
    )
    try:
        bridge.start()
    finally:
        if not args.no_monitor:
            restore_managed_mode(args.iface)


if __name__ == "__main__":
    main()
