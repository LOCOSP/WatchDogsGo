#!/usr/bin/env python3
"""
hs_capture.py — WatchDogsGo Handshake Capture Script
Standalone handshake/PMKID capture for AWUS036ACM (wlan2) via airodump-ng.
Dispatched by wdg_wifi_bridge.py on start_handshake command.

Prints SSID:name AP:bssid to stdout for each new capture — the bridge
streams these to the game to trigger the handshake event (200 XP + badge).

Usage (via bridge dispatch):
    python3 hs_capture.py --iface wlan2 --loot-dir /path/to/loot

Usage (standalone test):
    sudo python3 hs_capture.py --iface wlan2 --loot-dir ./loot

Dependencies: airodump-ng, hcxpcapngtool (system), running as root
"""

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time

log = logging.getLogger("hs_capture")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,  # stderr so stdout stays clean for bridge
)

# Seconds between hcxpcapngtool polls
POLL_INTERVAL = 10.0


def set_monitor_mode(iface: str) -> bool:
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=True,
                       capture_output=True)
        subprocess.run(["iw", "dev", iface, "set", "type", "monitor"], check=True,
                       capture_output=True)
        subprocess.run(["ip", "link", "set", iface, "up"], check=True,
                       capture_output=True)
        log.info("Set %s to monitor mode", iface)
        return True
    except subprocess.CalledProcessError as e:
        log.error("Failed to set monitor mode on %s: %s", iface, e)
        return False


def restore_managed_mode(iface: str):
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=False,
                       capture_output=True)
        subprocess.run(["iw", "dev", iface, "set", "type", "managed"], check=False,
                       capture_output=True)
        subprocess.run(["ip", "link", "set", iface, "up"], check=False,
                       capture_output=True)
        log.info("Restored %s to managed mode", iface)
    except Exception as e:
        log.warning("Could not restore managed mode on %s: %s", iface, e)


def check_tool(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


class HsCapture:
    def __init__(self, iface: str, loot_dir: str):
        self.iface = iface
        self.loot_dir = loot_dir
        self._proc = None
        self._running = False
        self._hs_file = ""
        self._hs_prefix = ""
        self._seen_bssids: set = set()

    def start(self):
        if not check_tool("airodump-ng"):
            print("error: airodump-ng not installed", flush=True)
            sys.exit(1)
        if not check_tool("hcxpcapngtool"):
            print("error: hcxpcapngtool not installed", flush=True)
            sys.exit(1)

        # Set up signal handlers for clean teardown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        hs_dir = os.path.join(self.loot_dir, "handshakes")
        os.makedirs(hs_dir, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        self._hs_prefix = os.path.join(hs_dir, f"hs_{ts}")
        # airodump-ng appends -01.cap to the prefix
        self._hs_file = self._hs_prefix + "-01.cap"

        # Check if already in monitor mode (e.g. left by deauth)
        check = subprocess.run(
            ["iw", "dev", self.iface, "info"],
            capture_output=True, text=True
        )
        already_monitor = "type monitor" in check.stdout
        if not already_monitor:
            if not set_monitor_mode(self.iface):
                print(f"error: could not set {self.iface} to monitor mode", flush=True)
                sys.exit(1)
        else:
            log.info("%s already in monitor mode — skipping mode set", self.iface)

        try:
            self._proc = subprocess.Popen(
                ["airodump-ng", self.iface,
                 "-w", self._hs_prefix,
                 "--output-format", "pcapng"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.error("airodump-ng failed to start: %s", e)
            print(f"error: airodump-ng failed: {e}", flush=True)
            restore_managed_mode(self.iface)
            sys.exit(1)

        self._running = True
        log.info("airodump-ng running on %s, saving to %s", self.iface, self._hs_file)
        print(f"handshake capture started on {self.iface}", flush=True)

        self._poll_loop()

    def _poll_loop(self):
        """Poll hcxpcapngtool every POLL_INTERVAL seconds for new hashes."""
        hash_file = self._hs_prefix + "-01.hc22000"

        while self._running:
            # Sleep in short intervals so SIGTERM wakes us quickly
            for _ in range(int(POLL_INTERVAL / 0.5)):
                if not self._running:
                    break
                time.sleep(0.5)

            if not self._running:
                break

            # Check if airodump-ng died unexpectedly
            if self._proc and self._proc.poll() is not None:
                log.warning("airodump-ng exited unexpectedly (rc=%d)", self._proc.returncode)
                print("error: airodump-ng exited unexpectedly", flush=True)
                break

            # Check for capture file
            cap_file = self._hs_prefix + "-01.cap"
            if not os.path.exists(cap_file):
                # Try pcapng extension
                cap_file = self._hs_prefix + "-01.pcapng"
                if not os.path.exists(cap_file):
                    log.debug("No capture file yet")
                    continue

            try:
                result = subprocess.run(
                    ["hcxpcapngtool", cap_file, "-o", hash_file],
                    capture_output=True, text=True, timeout=15
                )
                output = result.stdout + result.stderr

                eapol_m = re.search(
                    r"EAPOL pairs written to 22000 hash file[^:]*:\s*(\d+)", output)
                pmkid_m = re.search(
                    r"PMKID written to 22000 hash file[^:]*:\s*(\d+)", output)
                eapol_count = int(eapol_m.group(1)) if eapol_m else 0
                pmkid_count = int(pmkid_m.group(1)) if pmkid_m else 0

                log.debug("Poll: %d EAPOL, %d PMKID", eapol_count, pmkid_count)

                if os.path.exists(hash_file):
                    self._process_hash_file(hash_file)

            except subprocess.TimeoutExpired:
                log.warning("hcxpcapngtool timed out")
            except Exception as e:
                log.debug("Poll error: %s", e)

        self._cleanup()

    def _process_hash_file(self, hash_file: str):
        """Parse hc22000 file, emit SSID:/AP: lines, and write per-BSSID loot files."""
        try:
            with open(hash_file, "r") as f:
                lines = f.readlines()
        except Exception as e:
            log.debug("Hash file read error: %s", e)
            return

        for line in lines:
            parts = line.strip().split("*")
            if len(parts) < 4:
                continue

            # Handle both WPA* and 22000* hash formats
            if parts[0] == "WPA":
                if len(parts) < 6:
                    continue
                bssid_hex = parts[3]
                essid_hex = parts[5]
                cap_type = "EAPOL" if parts[1] == "02" else "PMKID"
            elif parts[0] in ("22000", "22301"):
                bssid_hex = parts[1]
                essid_hex = parts[3]
                cap_type = "PMKID" if parts[0] == "22301" else "EAPOL"
            else:
                continue

            # Validate bssid_hex before formatting
            if len(bssid_hex) < 12:
                log.debug("Skipping malformed hash line: bssid_hex=%r", bssid_hex)
                continue

            try:
                bssid = ":".join(
                    bssid_hex[i:i+2] for i in range(0, 12, 2)
                ).upper()
                essid = bytes.fromhex(essid_hex).decode(
                    "utf-8", errors="replace")
            except Exception:
                bssid = bssid_hex
                essid = ""

            if bssid not in self._seen_bssids:
                self._seen_bssids.add(bssid)
                log.info("New handshake: %s %s (%s)", bssid, essid, cap_type)

                # Write per-BSSID loot files matching loot_manager expectations:
                # handshakes/<ssid>_<bssid>.txt / .22000 / .pcap
                self._write_loot_files(bssid, essid, line.strip(), cap_type)

                # This line is what the bridge writes to the game
                print(f"SSID:{essid} AP:{bssid}", flush=True)

    def _find_active_session(self) -> str:
        """Find the most recently modified session directory in loot_dir."""
        import glob
        pattern = os.path.join(self.loot_dir, "????-??-??_??-??-??")
        sessions = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if sessions:
            return os.path.join(sessions[0], "handshakes")
        return os.path.join(self.loot_dir, "handshakes")

    def _write_loot_files(self, bssid: str, essid: str, hash_line: str, cap_type: str):
        """Write per-BSSID loot files matching loot_manager.py naming convention."""
        import shutil
        # Sanitize for filename
        safe_ssid = re.sub(r"[^\w\-]", "_", essid) if essid else "hidden"
        safe_bssid = bssid.replace(":", "")
        hs_dir = self._find_active_session()
        os.makedirs(hs_dir, exist_ok=True)
        base = os.path.join(hs_dir, f"{safe_ssid}_{safe_bssid}")

        # .txt metadata
        try:
            with open(base + ".txt", "w") as f:
                f.write(f"ssid={essid}\n")
                f.write(f"bssid={bssid}\n")
                f.write(f"type={cap_type}\n")
                f.write(f"captured={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"source=hs_capture.py\n")
            log.debug("Wrote %s.txt", base)
        except Exception as e:
            log.debug("Could not write .txt: %s", e)

        # .22000 hash file — just this BSSID's line
        try:
            with open(base + ".22000", "w") as f:
                f.write(hash_line + "\n")
            log.debug("Wrote %s.22000", base)
        except Exception as e:
            log.debug("Could not write .22000: %s", e)

        # .pcap — extract BSSID-filtered frames from combined cap via tshark
        cap_file = self._hs_prefix + "-01.cap"
        if not os.path.exists(cap_file):
            cap_file = self._hs_prefix + "-01.pcapng"
        if os.path.exists(cap_file) and shutil.which("tshark"):
            try:
                subprocess.run(
                    ["tshark", "-r", cap_file,
                     "-w", base + ".pcap",
                     "-Y", f"wlan.bssid == {bssid}"],
                    capture_output=True, timeout=30
                )
                log.debug("Wrote %s.pcap", base)
            except Exception as e:
                log.debug("tshark pcap extraction failed: %s", e)

    def _handle_signal(self, signum, frame):
        log.info("Signal %d received — stopping", signum)
        self._running = False

    def _cleanup(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        restore_managed_mode(self.iface)
        captured = len(self._seen_bssids)
        log.info("Capture complete: %d unique handshakes", captured)
        print(f"handshake capture stopped — {captured} captured", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="WatchDogsGo Handshake Capture — dispatched by wdg_wifi_bridge.py")
    parser.add_argument("--iface", default="wlan2",
                        help="Monitor mode interface (default: wlan2)")
    parser.add_argument("--loot-dir", default="",
                        help="Loot directory for captures")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("error: must run as root", file=sys.stderr)
        sys.exit(1)

    loot_dir = args.loot_dir or os.path.expanduser("~/python/WatchDogsGo/loot")

    capture = HsCapture(iface=args.iface, loot_dir=loot_dir)
    capture.start()


if __name__ == "__main__":
    main()
