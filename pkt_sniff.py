#!/usr/bin/env python3
"""
pkt_sniff.py — WatchDogsGo Packet Sniffer

Standalone 802.11 packet monitor, dispatched by wdg_wifi_bridge.py on the
start_sniffer / start_pkt_sniff command. Companion to hs_capture.py.

Usage (via bridge dispatch):
    python3 pkt_sniff.py --iface wdg0 --loot-dir /path/to/loot

Usage (standalone test):
    sudo python3 pkt_sniff.py --iface wdg0 --loot-dir ./loot

Dependencies: scapy (system/venv), iw, ip (system), running as root

Output contract — every line printed here is streamed verbatim to the game by
the bridge and lands in its terminal view via app.py's catch-all _term_add():

  * The startup line MUST contain "packet sniffer" (or "sniffer start"), which
    is what flips app.py's self.sniffing state on. See _handle_serial_line().
  * NEVER print a line starting with "SSID:" that also contains "AP:" — that is
    hs_capture.py's handshake signal and would fire a bogus 200 XP event.
  * NEVER print a line starting with '"' — that is the CSV scan-result format
    and would be parsed as a network list entry.
  * Print with flush=True. The bridge reads our stdout as a PIPE, so Python
    block-buffers by default and nothing would reach the game until we exit.

The periodic summary uses "<ssid>, CH<n>: <clients>" followed by bare client
MACs, which is the shape network_manager.parse_sniffer_results() documents.
That parser is currently unreferenced, but matching it keeps the format correct
if it is ever wired up, and it reads cleanly in the terminal meanwhile.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("pkt_sniff")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Seconds between periodic AP/client summaries
SUMMARY_INTERVAL = 15
# Seconds to dwell on each channel before hopping
DWELL = 0.5
# Stop appending to the pcap after this many frames, so a sniffer left running
# overnight cannot fill /mnt or the SD card.
MAX_PCAP_FRAMES = 200_000

# 2.4 GHz first — that is where most clients live. Unsupported channels are
# dropped from the rotation on first failure rather than retried forever.
CANDIDATE_CHANNELS = [1, 6, 11, 2, 3, 4, 5, 7, 8, 9, 10,
                      36, 40, 44, 48, 149, 153, 157, 161]


def check_tool(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def is_group_mac(mac: str) -> bool:
    """True for multicast/broadcast (group) addresses, which are not stations.

    The I/G bit is the low bit of the first octet, so 33:33:* (IPv6 multicast),
    01:00:5E:* (IPv4 multicast) and ff:ff:ff:ff:ff:ff all test true. Without
    this, every mDNS/SSDP frame an AP forwards is counted as a connected
    client and the per-AP client counts are meaningless.
    """
    try:
        return bool(int(mac.split(":")[0], 16) & 1)
    except (ValueError, IndexError, AttributeError):
        return True


def clean_ssid(ssid: str) -> str:
    """Strip the NUL padding APs use to cloak an SSID, plus any control chars.

    A cloaked SSID is not empty, it is \x00 repeated, so a plain truthiness
    test renders it as blank rather than as hidden."""
    return "".join(c for c in ssid if c.isprintable()).strip()


def set_monitor_mode(iface: str) -> bool:
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=True,
                       capture_output=True)
        subprocess.run(["iw", "dev", iface, "set", "type", "monitor"],
                       check=True, capture_output=True)
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
        subprocess.run(["iw", "dev", iface, "set", "type", "managed"],
                       check=False, capture_output=True)
        subprocess.run(["ip", "link", "set", iface, "up"], check=False,
                       capture_output=True)
        # airodump-ng style captures leave these set; clear them so the
        # interface goes back to a clean managed state for the next scan.
        subprocess.run(["ip", "link", "set", iface, "promisc", "off",
                        "allmulticast", "off"], check=False, capture_output=True)
        log.info("Restored %s to managed mode", iface)
    except Exception as e:
        log.warning("Could not restore managed mode on %s: %s", iface, e)


def _iface_carries_default_route(iface: str) -> bool:
    """Refuse to monitor-mode the interface holding the default route.

    wdg_wifi_bridge.py has this guard; hs_capture.py does not. Losing the
    uplink mid-session is a much worse failure than refusing to start.
    """
    try:
        result = subprocess.run(["ip", "route", "show", "default"],
                                capture_output=True, text=True, timeout=5)
        return f" dev {iface} " in result.stdout
    except Exception:
        return False


class PktSniff:
    def __init__(self, iface: str, loot_dir: str):
        self.iface = iface
        self.loot_dir = loot_dir
        self.running = False
        self._sniffer = None
        self._writer = None
        self._hopper = None
        self._lock = threading.Lock()
        # Only put the radio back to managed if WE were the one that took it to
        # monitor. There is a single capture radio on this box, so hs_capture.py
        # may already own it — restoring on our way out would silently kill a
        # handshake capture that is still running.
        self._set_monitor = False

        self.aps = {}          # bssid -> {ssid, channel, rssi, packets}
        self.clients = {}      # station mac -> bssid
        self.frames = 0
        self.deauths = 0
        self._channels = list(CANDIDATE_CHANNELS)
        self._channel = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        for tool in ("iw", "ip"):
            if not check_tool(tool):
                print(f"error: {tool} not installed", flush=True)
                return False
        try:
            from scapy.all import AsyncSniffer  # noqa: F401
        except ImportError:
            print("error: scapy not installed", flush=True)
            return False

        if not os.path.exists(f"/sys/class/net/{self.iface}"):
            print(f"error: {self.iface} not found — plug in the WiFi adapter",
                  flush=True)
            return False

        if _iface_carries_default_route(self.iface):
            print(f"error: {self.iface} carries the default route — refusing "
                  f"to enable monitor mode on the uplink", flush=True)
            return False

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        if not self._already_monitor():
            if not set_monitor_mode(self.iface):
                print(f"error: could not set {self.iface} to monitor mode",
                      flush=True)
                return False
            self._set_monitor = True
        else:
            log.info("%s already in monitor mode — leaving mode alone "
                     "(another capture may own it)", self.iface)

        self._open_pcap()
        self.running = True

        from scapy.all import AsyncSniffer
        try:
            self._sniffer = AsyncSniffer(iface=self.iface, prn=self._on_frame,
                                         store=False)
            self._sniffer.start()
        except Exception as e:
            log.error("scapy failed to start on %s: %s", self.iface, e)
            print(f"error: packet sniffer failed to start: {e}", flush=True)
            if self._set_monitor:
                restore_managed_mode(self.iface)
            return False

        self._hopper = threading.Thread(target=self._hop_channels, daemon=True)
        self._hopper.start()

        # This exact wording matters: app.py sets self.sniffing when a serial
        # line contains "packet sniffer" or "sniffer start".
        print(f"packet sniffer started on {self.iface}", flush=True)
        return True

    def _already_monitor(self) -> bool:
        try:
            out = subprocess.run(["iw", "dev", self.iface, "info"],
                                 capture_output=True, text=True, timeout=5)
            return "type monitor" in out.stdout
        except Exception:
            return False

    def _open_pcap(self):
        if not self.loot_dir:
            return
        try:
            from scapy.all import PcapWriter
            d = os.path.join(self.loot_dir, "sniffer")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(
                d, "pkt_%s.pcap" % time.strftime("%Y%m%d_%H%M%S"))
            # append=False, sync=False: buffered writes, flushed on close.
            self._writer = PcapWriter(path, append=False, sync=False)
            self._pcap_path = path
            log.info("Writing capture to %s", path)
        except Exception as e:
            log.warning("Could not open pcap for writing: %s", e)
            self._writer = None

    def _handle_signal(self, signum, _frame):
        log.info("Signal %s received — stopping", signum)
        self.running = False

    # ------------------------------------------------------------------
    # Channel hopping
    # ------------------------------------------------------------------

    def _hop_channels(self):
        while self.running:
            if not self._channels:
                time.sleep(DWELL)
                continue
            ch = self._channels[self._channel % len(self._channels)]
            self._channel += 1
            try:
                r = subprocess.run(["iw", "dev", self.iface, "set", "channel",
                                    str(ch)], capture_output=True, timeout=5)
                if r.returncode != 0:
                    # Regulatory domain or the radio does not allow it — drop
                    # it from the rotation instead of failing on it every pass.
                    log.debug("dropping unsupported channel %s", ch)
                    with self._lock:
                        if ch in self._channels:
                            self._channels.remove(ch)
                    continue
            except Exception:
                pass
            # Sleep in short slices so SIGTERM is acted on quickly.
            waited = 0.0
            while self.running and waited < DWELL:
                time.sleep(0.1)
                waited += 0.1

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    def _on_frame(self, pkt):
        from scapy.layers.dot11 import (Dot11, Dot11Beacon, Dot11ProbeResp,
                                        Dot11Deauth, Dot11Elt)
        if not pkt.haslayer(Dot11):
            return
        self.frames += 1

        if self._writer is not None and self.frames <= MAX_PCAP_FRAMES:
            try:
                self._writer.write(pkt)
            except Exception:
                pass

        dot11 = pkt.getlayer(Dot11)
        rssi = getattr(pkt, "dBm_AntSignal", None)

        # --- Access points, from beacons and probe responses ---
        if pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp):
            bssid = (dot11.addr3 or "").upper()
            if not bssid:
                return
            ssid = ""
            elt = pkt.getlayer(Dot11Elt)
            while isinstance(elt, Dot11Elt):
                if elt.ID == 0:
                    try:
                        ssid = clean_ssid(elt.info.decode("utf-8", "replace"))
                    except Exception:
                        ssid = ""
                    break
                elt = elt.payload.getlayer(Dot11Elt)
            ch = self._channel_of(pkt)
            with self._lock:
                new = bssid not in self.aps
                ap = self.aps.setdefault(
                    bssid, {"ssid": ssid, "channel": ch, "rssi": rssi,
                            "packets": 0})
                ap["packets"] += 1
                if ssid:
                    ap["ssid"] = ssid
                if ch:
                    ap["channel"] = ch
                if rssi is not None:
                    ap["rssi"] = rssi
            if new:
                label = ssid if ssid else "<hidden>"
                sig = f" {rssi:>4}dBm" if rssi is not None else ""
                print(f"[SNF] AP  {label[:20]:<20} ch{ch or 0:<3} "
                      f"{bssid}{sig}", flush=True)
            return

        # --- Deauthentication: the interesting hostile traffic ---
        if pkt.haslayer(Dot11Deauth):
            self.deauths += 1
            src = (dot11.addr2 or "??").upper()
            dst = (dot11.addr1 or "??").upper()
            print(f"[SNF] DEAUTH {src} -> {dst}", flush=True)
            return

        # --- Associated clients, from data frames ---
        if dot11.type == 2:  # data
            self._note_client(dot11)

    def _note_client(self, dot11):
        # to-DS: addr1=BSSID addr2=station. from-DS: addr1=station addr2=BSSID.
        to_ds = dot11.FCfield & 0x1 != 0
        from_ds = dot11.FCfield & 0x2 != 0
        if to_ds and not from_ds:
            bssid, sta = dot11.addr1, dot11.addr2
        elif from_ds and not to_ds:
            bssid, sta = dot11.addr2, dot11.addr1
        else:
            return
        if not bssid or not sta:
            return
        bssid, sta = bssid.upper(), sta.upper()
        if sta == bssid or is_group_mac(sta) or sta == "00:00:00:00:00:00":
            return
        with self._lock:
            if sta in self.clients:
                return
            self.clients[sta] = bssid
            ssid = self.aps.get(bssid, {}).get("ssid", "")
        label = f" ({ssid[:20]})" if ssid else ""
        print(f"[SNF] STA {sta} -> {bssid}{label}", flush=True)

    def _channel_of(self, pkt):
        """Channel from the DS Parameter Set element, else from radiotap freq."""
        from scapy.layers.dot11 import Dot11Elt
        elt = pkt.getlayer(Dot11Elt)
        while isinstance(elt, Dot11Elt):
            if elt.ID == 3 and elt.info:
                try:
                    return elt.info[0]
                except Exception:
                    break
            elt = elt.payload.getlayer(Dot11Elt)
        freq = getattr(pkt, "ChannelFrequency", None)
        if freq:
            if 2412 <= freq <= 2484:
                return 14 if freq == 2484 else (freq - 2407) // 5
            if 5000 < freq < 5900:
                return (freq - 5000) // 5
        return 0

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _summary(self):
        with self._lock:
            aps = dict(self.aps)
            clients = dict(self.clients)
        if not aps:
            print(f"[SNF] {self.frames} frames, no APs yet", flush=True)
            return
        by_ap = {}
        for sta, bssid in clients.items():
            by_ap.setdefault(bssid, []).append(sta)
        print(f"[SNF] --- {self.frames} frames | {len(aps)} APs | "
              f"{len(clients)} clients | {self.deauths} deauths ---",
              flush=True)
        # Busiest first, capped so one summary cannot flood the terminal view.
        for bssid, ap in sorted(aps.items(),
                                key=lambda kv: (len(by_ap.get(kv[0], [])),
                                                kv[1]["packets"]),
                                reverse=True)[:10]:
            stations = by_ap.get(bssid, [])
            ssid = ap["ssid"] or "<hidden>"
            print(f"{ssid}, CH{ap['channel'] or 0}: {len(stations)}",
                  flush=True)
            for sta in stations[:8]:
                print(sta, flush=True)

    def run(self):
        last = time.time()
        try:
            while self.running:
                time.sleep(0.2)
                if time.time() - last >= SUMMARY_INTERVAL:
                    last = time.time()
                    self._summary()
                if self._sniffer is not None and not self._sniffer.running:
                    print("error: packet sniffer stopped unexpectedly",
                          flush=True)
                    break
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None
        if self._writer is not None:
            try:
                self._writer.close()
                log.info("Capture written to %s", getattr(self, "_pcap_path", "?"))
            except Exception:
                pass
            self._writer = None
        self._write_summary_file()
        if self._set_monitor:
            restore_managed_mode(self.iface)
        else:
            log.info("Leaving %s in monitor mode — we did not set it",
                     self.iface)
        print(f"packet sniffer stopped — {self.frames} frames, "
              f"{len(self.aps)} APs, {len(self.clients)} clients", flush=True)

    def _write_summary_file(self):
        if not self.loot_dir or not self.aps:
            return
        try:
            d = os.path.join(self.loot_dir, "sniffer")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(
                d, "pkt_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
            by_ap = {}
            for sta, bssid in self.clients.items():
                by_ap.setdefault(bssid, []).append(sta)
            with open(path, "w") as f:
                f.write(f"frames={self.frames} aps={len(self.aps)} "
                        f"clients={len(self.clients)} deauths={self.deauths}\n\n")
                for bssid, ap in sorted(self.aps.items(),
                                        key=lambda kv: (len(by_ap.get(kv[0], [])),
                                                        kv[1]["packets"]),
                                        reverse=True):
                    f.write(f"{ap['ssid'] or '<hidden>'}, CH{ap['channel'] or 0}: "
                            f"{len(by_ap.get(bssid, []))}  [{bssid}] "
                            f"packets={ap['packets']}\n")
                    for sta in by_ap.get(bssid, []):
                        f.write(f"{sta}\n")
            log.info("Summary written to %s", path)
        except Exception as e:
            log.warning("Could not write summary: %s", e)


def main():
    parser = argparse.ArgumentParser(description="WatchDogsGo packet sniffer")
    # Defaults to wdg0, the AC1200 pinned by 10-wdg-ac1200.link on this box.
    # hs_capture.py still defaults to wlan2 (an AWUS036ACM that is not fitted);
    # the bridge always passes --iface explicitly, so this only affects manual runs.
    parser.add_argument("--iface", default="wdg0",
                        help="Monitor mode interface (default: wdg0)")
    parser.add_argument("--loot-dir", default="",
                        help="Directory to write pcap + summary into")
    args = parser.parse_args()

    loot_dir = args.loot_dir or ""
    sniffer = PktSniff(iface=args.iface, loot_dir=loot_dir)
    if not sniffer.start():
        sys.exit(1)
    sniffer.run()


if __name__ == "__main__":
    main()
