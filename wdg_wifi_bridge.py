#!/usr/bin/env python3
"""
WatchDogsGo Linux WiFi + BLE Bridge
Emulates an ESP32 projectZero device over a virtual serial port,
using the host's WiFi adapter for scanning and built-in BT for BLE.

Usage:
    sudo python3 wdg_wifi_bridge.py --iface wlan1 --bt-iface hci0 --sniffer-iface wlan2 --pty /tmp/esp32-pty

Then launch game with:
    sudo ./run.sh /tmp/esp32-pty

Changes from original (FusedStamen fork):
  - BLE fast-fail detection: times each scan and logs a warning if it completes
    in under 2 seconds with 0 results, indicating the Bluetooth adapter has dropped
  - Packet sniffer: start_sniffer/stop_sniffer commands put wlan2 (AWUS036ACM)
    in monitor mode and capture raw 802.11 frames to pcapng via tcpdump,
    saved to the loot session directory
  - Handshake capture: start_handshake/start_handshake_serial commands use
    airodump-ng on wlan2 for WPA handshake and PMKID capture; polls with
    hcxpcapngtool every 10 seconds to detect new hashes and fires the game's
    handshake event (SSID:/AP: format) triggering 200 XP + badge + map marker
  - wlan1 restore: after stopping handshake capture, wlan1 is brought back up
    since airodump-ng can bring it down during capture
  - --sniffer-iface: new argument for dedicated capture interface (default wlan2),
    separate from the wardriving interface (wlan1)
  - --loot-dir: new argument to specify where packet captures are saved

Dependencies: bleak, airodump-ng, hcxpcapngtool, tcpdump, iw
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
        log.warning("iw scan timed out")
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
    def __init__(self, iface: str, pty_path: str, bt_iface: str = "hci0",
                 sniffer_iface: str = "wlan2", loot_dir: str = ""):
        self.iface = iface
        self.pty_path = pty_path
        self.bt_iface = bt_iface
        self.sniffer_iface = sniffer_iface
        self.loot_dir = loot_dir or os.path.expanduser("~/python/WatchDogsGo/loot")
        self._master_fd = None
        self._slave_fd = None
        self._running = False
        self._scan_requested = False
        self._lock = threading.Lock()
        # Packet sniff state
        self._pkt_proc = None
        self._pkt_sniff_active = False
        self._pkt_count = 0
        self._pkt_file = ""
        # HS capture state
        self._hs_proc = None
        self._hs_active = False
        self._hs_file = ""
        self._hs_count = 0

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
        self._stop_pkt_sniff()
        self._stop_hs_capture()
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
            self._stop_pkt_sniff()
            self._stop_hs_capture()
            self._write("all stopped\r\n")
        elif cmd == "scan_bt":
            threading.Thread(target=self._do_ble_scan, daemon=True).start()
        elif cmd in ("start_pkt_sniff", "start_sniffer"):
            threading.Thread(target=self._do_pkt_sniff, daemon=True).start()
        elif cmd in ("stop_pkt_sniff", "stop_sniffer", "sniffer_stop"):
            self._stop_pkt_sniff()
        elif cmd in ("start_handshake", "start_handshake_serial"):
            threading.Thread(target=self._do_hs_capture, daemon=True).start()
        elif cmd == "stop_handshake":
            self._stop_hs_capture()
        else:
            log.debug("Unknown command: %s", cmd)

    def _do_scan(self):
        log.info("Scanning on %s...", self.iface)
        networks = scan_wifi(self.iface)
        log.info("Found %d networks", len(networks))
        for i, net in enumerate(networks):
            self._write(format_network_csv(i, net))
            time.sleep(0.01)
        self._write("scan results printed\r\n")

    def _do_ble_scan(self):
        log.info("BLE scanning on %s...", self.bt_iface)
        t_start = time.time()
        devices = scan_ble(self.bt_iface, duration=8.0)
        elapsed = time.time() - t_start
        # Fast-fail detection: if scan completed in under 2s with 0 results
        # the adapter has likely dropped out
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
    # Packet sniffer (AWUS036ACM / wlan2 in monitor mode via tcpdump)
    # ------------------------------------------------------------------

    def _do_pkt_sniff(self):
        if self._pkt_sniff_active:
            self._write("pkt_sniff already running\r\n")
            return
        log.info("Starting packet sniff on %s", self.sniffer_iface)
        if not set_monitor_mode(self.sniffer_iface):
            self._write(f"pkt_sniff error: could not set {self.sniffer_iface} to monitor mode\r\n")
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(self.loot_dir, exist_ok=True)
        self._pkt_file = os.path.join(self.loot_dir, f"pkt_sniff_{ts}.pcapng")
        try:
            self._pkt_proc = subprocess.Popen(
                ["tcpdump", "-i", self.sniffer_iface, "-w", self._pkt_file,
                 "--immediate-mode", "-U"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            log.error("tcpdump failed to start: %s", e)
            self._write(f"pkt_sniff error: {e}\r\n")
            restore_managed_mode(self.sniffer_iface)
            return
        self._pkt_sniff_active = True
        self._pkt_count = 0
        self._write(f"sniffer start — saving to {self._pkt_file}\r\n")
        log.info("tcpdump running, saving to %s", self._pkt_file)
        threading.Thread(target=self._pkt_counter_loop, daemon=True).start()

    def _pkt_counter_loop(self):
        if not self._pkt_proc:
            return
        try:
            for line in self._pkt_proc.stderr:
                if not self._pkt_sniff_active:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                log.debug("tcpdump: %s", text)
                m = re.search(r"(\d+) packets captured", text)
                if m:
                    self._pkt_count = int(m.group(1))
                    self._write(f"pkt_count {self._pkt_count}\r\n")
        except Exception:
            pass

    def _stop_pkt_sniff(self):
        if not self._pkt_sniff_active:
            return
        self._pkt_sniff_active = False
        if self._pkt_proc:
            try:
                self._pkt_proc.terminate()
                self._pkt_proc.wait(timeout=3)
            except Exception:
                try:
                    self._pkt_proc.kill()
                except Exception:
                    pass
            self._pkt_proc = None
        restore_managed_mode(self.sniffer_iface)
        self._write(f"pkt_sniff stopped — saved to {self._pkt_file}\r\n")
        log.info("Packet sniff stopped, file: %s", self._pkt_file)

    # ------------------------------------------------------------------
    # Handshake capture (AWUS036ACM / wlan2 via hcxdumptool)
    # ------------------------------------------------------------------

    def _do_hs_capture(self):
        if self._hs_active:
            self._write("handshake capture already running\r\n")
            return
        if not self._check_tool("airodump-ng"):
            self._write("handshake error: airodump-ng not installed\r\n")
            return
        log.info("Starting HS capture on %s", self.sniffer_iface)
        ts = time.strftime("%Y%m%d_%H%M%S")
        hs_dir = os.path.join(self.loot_dir, "handshakes")
        os.makedirs(hs_dir, exist_ok=True)
        # airodump-ng appends -01.pcapng so we give it a prefix
        self._hs_prefix = os.path.join(hs_dir, f"hs_{ts}")
        self._hs_file = self._hs_prefix + "-01.cap"
        self._hs_count = 0
        # Put interface in monitor mode first
        if not set_monitor_mode(self.sniffer_iface):
            self._write(f"handshake error: could not set {self.sniffer_iface} to monitor mode\r\n")
            return
        try:
            self._hs_proc = subprocess.Popen(
                ["airodump-ng", self.sniffer_iface,
                 "-w", self._hs_prefix,
                 "--output-format", "pcapng"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.error("airodump-ng failed to start: %s", e)
            self._write(f"handshake error: {e}\r\n")
            restore_managed_mode(self.sniffer_iface)
            return
        self._hs_active = True
        self._write(f"handshake capture started — saving to {self._hs_file}\r\n")
        log.info("airodump-ng running, saving to %s", self._hs_file)
        threading.Thread(target=self._hs_output_loop, daemon=True).start()

    def _hs_output_loop(self):
        """Poll hcxpcapngtool every 10s to detect new handshakes/PMKIDs."""
        poll_interval = 10.0
        last_count = 0
        seen_bssids = set()
        hash_file = self._hs_file + ".hc22000"

        while self._hs_active:
            time.sleep(poll_interval)
            if not self._hs_active:
                break
            if not os.path.exists(self._hs_file):
                continue
            try:
                result = subprocess.run(
                    ["hcxpcapngtool", self._hs_file, "-o", hash_file],
                    capture_output=True, text=True, timeout=15
                )
                output = result.stdout + result.stderr
                eapol_m = re.search(r"EAPOL pairs written to 22000 hash file[^:]*:\s*(\d+)", output)
                pmkid_m = re.search(r"PMKID written to 22000 hash file[^:]*:\s*(\d+)", output)
                eapol_count = int(eapol_m.group(1)) if eapol_m else 0
                pmkid_count = int(pmkid_m.group(1)) if pmkid_m else 0
                total = eapol_count + pmkid_count

                if total > last_count:
                    new_captures = total - last_count
                    last_count = total
                    log.info("New hashes: %d EAPOL + %d PMKID", eapol_count, pmkid_count)

                    if os.path.exists(hash_file):
                        try:
                            with open(hash_file, "r") as hf:
                                for line in hf:
                                    parts = line.strip().split("*")
                                    if len(parts) >= 4:
                                        bssid_hex = parts[1]
                                        essid_hex = parts[3]
                                        try:
                                            bssid = ":".join(
                                                bssid_hex[i:i+2]
                                                for i in range(0, 12, 2)
                                            ).upper()
                                            essid = bytes.fromhex(essid_hex).decode("utf-8", errors="replace")
                                        except Exception:
                                            bssid = bssid_hex
                                            essid = ""
                                        if bssid not in seen_bssids:
                                            seen_bssids.add(bssid)
                                            self._hs_count += 1
                                            cap_type = "PMKID" if parts[0].startswith("22301") else "EAPOL"
                                            log.info("Handshake: %s %s (%s)", bssid, essid, cap_type)
                                            # Game triggers on: line starts with SSID: and contains AP:
                                            self._write(f"SSID:{essid} AP:{bssid}\r\n")
                        except Exception as e:
                            log.debug("Hash file parse error: %s", e)
                            for _ in range(new_captures):
                                self._write("SSID:unknown AP:00:00:00:00:00:00\r\n")
                                self._hs_count += 1
            except subprocess.TimeoutExpired:
                log.warning("hcxpcapngtool timed out")
            except Exception as e:
                log.debug("HS poll error: %s", e)

    def _stop_hs_capture(self):
        if not self._hs_active:
            return
        self._hs_active = False
        if self._hs_proc:
            try:
                self._hs_proc.terminate()
                self._hs_proc.wait(timeout=3)
            except Exception:
                try:
                    self._hs_proc.kill()
                except Exception:
                    pass
            self._hs_proc = None
        # Restore wlan2 to managed mode and bring wlan1 back up
        # (hcxdumptool takes down all interfaces during capture)
        restore_managed_mode(self.sniffer_iface)
        subprocess.run(["ip", "link", "set", self.iface, "up"], check=False)
        self._write(
            f"handshake capture stopped — {self._hs_count} captured, "
            f"saved to {self._hs_file}\r\n")
        log.info("HS capture stopped, %d captured, file: %s", self._hs_count, self._hs_file)

    @staticmethod
    def _check_tool(name: str) -> bool:
        import shutil
        return shutil.which(name) is not None


def main():
    parser = argparse.ArgumentParser(description="WatchDogsGo Linux WiFi + BLE Bridge")
    parser.add_argument("--iface", default="wlan1",
                        help="WiFi interface for scanning (default: wlan1)")
    parser.add_argument("--bt-iface", default="hci0",
                        help="Bluetooth interface for BLE scanning (default: hci0)")
    parser.add_argument("--sniffer-iface", default="wlan2",
                        help="WiFi interface for packet sniff/HS capture (default: wlan2)")
    parser.add_argument("--pty", default="/tmp/esp32-pty",
                        help="PTY symlink path for WatchDogsGo (default: /tmp/esp32-pty)")
    parser.add_argument("--no-monitor", action="store_true",
                        help="Skip setting monitor mode on --iface (wlan1)")
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
