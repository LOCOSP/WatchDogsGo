"""Whitelist manager — persistent ignore list for WiFi/BLE devices.

Stores entries in a JSON file. Devices on the whitelist are silently
skipped during scans, wardriving, and attack target selection.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)


@dataclass
class WhitelistEntry:
    """Single whitelist entry."""
    type: str       # "wifi" or "ble"
    mac: str        # uppercase MAC (AA:BB:CC:DD:EE:FF)
    name: str       # SSID (wifi) or device name (ble)
    added_date: str  # ISO format


class WhitelistManager:
    """Load/save/query a persistent device whitelist."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: List[WhitelistEntry] = []
        self._mac_set: set[str] = set()
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load whitelist from JSON file."""
        self._entries.clear()
        self._mac_set.clear()
        if not self._path.exists():
            return
        try:
            with open(self._path, "r") as fh:
                data = json.load(fh)
            for item in data:
                entry = WhitelistEntry(
                    type=item.get("type", "ble"),
                    mac=item.get("mac", "").upper(),
                    name=item.get("name", ""),
                    added_date=item.get("added_date", ""),
                )
                if entry.mac:
                    self._entries.append(entry)
                    self._mac_set.add(entry.mac)
            log.info("Whitelist loaded: %d entries from %s", len(self._entries), self._path)
        except Exception as exc:
            log.warning("Cannot load whitelist %s: %s", self._path, exc)

    def save(self) -> None:
        """Save whitelist to JSON file."""
        try:
            data = [asdict(e) for e in self._entries]
            with open(self._path, "w") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            log.warning("Cannot save whitelist %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, entry_type: str, mac: str, name: str) -> bool:
        """Add a device. Returns False if MAC already exists."""
        mac = mac.upper().strip()
        if not mac:
            return False
        if mac in self._mac_set:
            return False
        entry = WhitelistEntry(
            type=entry_type,
            mac=mac,
            name=name.strip(),
            added_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        self._entries.append(entry)
        self._mac_set.add(mac)
        self.save()
        return True

    def remove(self, index: int) -> bool:
        """Remove entry by index. Returns False if out of range."""
        if index < 0 or index >= len(self._entries):
            return False
        entry = self._entries.pop(index)
        self._mac_set.discard(entry.mac)
        self.save()
        return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def is_blocked(self, mac: str) -> bool:
        """O(1) check if a MAC address is whitelisted."""
        return mac.upper() in self._mac_set

    @property
    def entries(self) -> List[WhitelistEntry]:
        return self._entries

    @property
    def count(self) -> int:
        return len(self._entries)
