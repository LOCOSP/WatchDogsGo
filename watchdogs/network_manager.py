"""Network scan result parsing and management."""

import re
from typing import Optional

from .app_state import AppState, Network


class NetworkManager:
    """Parse ESP32 scan output and manage the network list in AppState."""

    def __init__(self, state: AppState) -> None:
        self.state = state

    def parse_network_line(self, line: str) -> Optional[Network]:
        """Parse a CSV network line from ESP32 output.

        Expected format:
        "index","ssid","vendor","bssid","channel","auth","rssi","band"
        """
        if not line.startswith('"'):
            return None
        try:
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) < 8:
                return None
            return Network(
                index=parts[0],
                ssid=parts[1] if parts[1] else "<hidden>",
                vendor=parts[2],
                bssid=parts[3],
                channel=parts[4],
                auth=parts[5],
                rssi=parts[6],
                band=parts[7],
            )
        except Exception:
            return None

    def add_network(self, line: str) -> bool:
        """Parse line and append to state.networks. Returns True if added."""
        net = self.parse_network_line(line)
        if net:
            self.state.networks.append(net)
            return True
        return False

    def clear(self) -> None:
        self.state.networks.clear()
        self.state.scan_done = False

    @staticmethod
    def rssi_level(rssi_str: str) -> str:
        """Return 'good', 'fair', or 'weak' for palette mapping."""
        try:
            val = int(rssi_str.replace("dBm", "").strip())
        except (ValueError, AttributeError):
            return "weak"
        if val >= -50:
            return "good"
        if val >= -70:
            return "fair"
        return "weak"

    # ------------------------------------------------------------------
    # Sniffer result parsing
    # ------------------------------------------------------------------

    _ap_re = re.compile(r"^(.+),\s*CH(\d+):\s*(\d+)")
    _mac_re = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
    _probe_re = re.compile(r"^(.+?)\s*\(([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\)")
    _pkt_count_re = re.compile(r"count[:\s]+(\d+)", re.IGNORECASE)

    def parse_sniffer_results(self, lines: list[str]) -> None:
        """Parse ``show_sniffer_results`` output into state.sniffer_aps."""
        from .app_state import SnifferAP

        self.state.sniffer_aps.clear()
        current_ap: Optional[SnifferAP] = None

        for line in lines:
            m = self._ap_re.match(line)
            if m:
                current_ap = SnifferAP(
                    ssid=m.group(1).strip(),
                    channel=int(m.group(2)),
                    client_count=int(m.group(3)),
                )
                self.state.sniffer_aps.append(current_ap)
                continue

            if current_ap and self._mac_re.match(line):
                current_ap.clients.append(line.strip())

    def parse_probes(self, lines: list[str]) -> None:
        """Parse ``show_probes`` output into state.sniffer_probes."""
        from .app_state import ProbeEntry

        self.state.sniffer_probes.clear()
        for line in lines:
            m = self._probe_re.match(line)
            if m:
                self.state.sniffer_probes.append(
                    ProbeEntry(ssid=m.group(1).strip(), mac=m.group(2))
                )

    def extract_packet_count(self, line: str) -> Optional[int]:
        """Try to extract a cumulative sniffer packet count from *line*."""
        patterns = [
            r"(\d+)\s+packets?",
            r"captured[:\s]+(\d+)",
            r"total[:\s]+(\d+)",
            r"pkts[:\s]+(\d+)",
            r"count[:\s]+(\d+)",
            r"packets captured[:\s]+(\d+)",
            r"\bpkt\s*#?\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None
