"""Loot manager — persists all captured data to disk.

Session directory layout:
    <app_dir>/loot/<YYYY-MM-DD_HH-MM-SS>/
        serial_full.log          – every line from ESP32 (timestamped)
        scan_results.csv         – networks found during scan
        sniffer_aps.csv          – access points from sniffer
        sniffer_probes.csv       – probe requests captured
        handshakes/
            <ssid>_<bssid>.txt   – handshake metadata from serial
            <ssid>_<bssid>.pcap  – real pcap (from start_handshake_serial)
            <ssid>_<bssid>.hccapx – hashcat format (from start_handshake_serial)
            <ssid>_<bssid>.22000 – hc22000 hash (auto-generated from complete hccapx)
        portal_passwords.log     – portal form submissions
        evil_twin_capture.log    – evil twin captured data
        attacks.log              – attack start/stop events
"""

import base64
import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .app_state import AppState, Network, SnifferAP, ProbeEntry

log = logging.getLogger(__name__)


def _fsync_file(fh) -> None:
    """Flush Python buffer + OS page cache to physical disk.

    Without this, `with open(...).write(...)` only pushes bytes down to the
    OS cache. If the uConsole battery dies before Linux lazily writes that
    cache back to the eMMC/SD, the data is gone. fsync makes the write
    survive a hard power cut. Costs ~5–20 ms on SD — cheap per event,
    expensive per frame, so call it only from discrete save-on-event paths.
    """
    try:
        fh.flush()
        os.fsync(fh.fileno())
    except OSError:
        pass


def _sync_append(path: Path, text: str, encoding: str = "utf-8",
                 newline: Optional[str] = None) -> None:
    """Append text to path and fsync. Silently ignores I/O errors."""
    try:
        kwargs = {"encoding": encoding}
        if newline is not None:
            kwargs["newline"] = newline
        with open(path, "a", **kwargs) as fh:
            fh.write(text)
            _fsync_file(fh)
    except OSError:
        pass


def _sync_write(path: Path, text: str, encoding: str = "utf-8",
                newline: Optional[str] = None) -> None:
    """Write text to path and fsync. Silently ignores I/O errors."""
    try:
        kwargs = {"encoding": encoding}
        if newline is not None:
            kwargs["newline"] = newline
        with open(path, "w", **kwargs) as fh:
            fh.write(text)
            _fsync_file(fh)
    except OSError:
        pass


class LootManager:
    """Manages a per-session loot directory and auto-saves captured data."""

    @property
    def session_dir(self) -> str:
        return str(self._session)

    def __init__(self, app_dir: str, gps_manager=None) -> None:
        self._gps = gps_manager  # Optional GpsManager for geo-tagging
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._base = Path(app_dir) / "loot"
        self._session = self._base / ts
        self._serial_fh: Optional[io.TextIOWrapper] = None
        self._handshake_dir: Optional[Path] = None
        self._session_active = False

        # Handshake metadata parser state (from serial log keywords)
        self._hs_buffer: List[str] = []
        self._hs_collecting = False

        # PCAP base64 parser state (from start_handshake_serial command)
        # Firmware outputs:
        #   --- PCAP BEGIN ---
        #   <base64 lines>
        #   --- PCAP END ---
        #   PCAP_SIZE: <N>
        #   --- HCCAPX BEGIN ---
        #   <base64 lines>
        #   --- HCCAPX END ---
        #   SSID: <ssid>  AP: <bssid>
        self._pcap_collecting = False
        self._pcap_b64_lines: List[str] = []
        self._hccapx_collecting = False
        self._hccapx_b64_lines: List[str] = []
        self._pcap_meta_ssid = "unknown"
        self._pcap_meta_bssid = "unknown"

        # Aggregate loot database
        self._db_path = self._base / "loot_db.json"
        self._db: dict = {}
        self._session_key = ts

        try:
            self._session.mkdir(parents=True, exist_ok=True)
            (self._session / "handshakes").mkdir(exist_ok=True)
            self._handshake_dir = self._session / "handshakes"
            self._serial_fh = open(
                self._session / "serial_full.log", "a", encoding="utf-8"
            )
            self._session_active = True
            log.info("Loot session: %s", self._session)
        except OSError as exc:
            log.error("Cannot create loot directory: %s", exc)

        # Load or build aggregate loot database
        self._db = self._load_or_build_db()

        # Periodic background sync — protects against power loss between
        # event-driven fsyncs. Every BACKUP_INTERVAL seconds it fsyncs the
        # open serial log handle and calls os.sync() so everything still
        # sitting in the OS page cache gets pushed to the SD card.
        self._backup_stop = False
        self._backup_thread = threading.Thread(
            target=self._periodic_backup_loop,
            name="loot-backup",
            daemon=True,
        )
        self._backup_thread.start()

    BACKUP_INTERVAL = 30  # seconds between sync passes

    def _periodic_backup_loop(self) -> None:
        """Run on a daemon thread; fsync the serial log and os.sync the FS.

        os.sync() is a syscall that schedules all dirty pages across every
        mounted filesystem to be written out. It can take ~100 ms–2 s on a
        busy SD card, which is why it runs off the game loop thread.
        """
        while not self._backup_stop:
            # Use short sleeps so shutdown is responsive
            for _ in range(self.BACKUP_INTERVAL):
                if self._backup_stop:
                    return
                time.sleep(1)
            self.flush_all()

    def flush_all(self) -> None:
        """Force every buffered write to reach the SD card.

        Safe to call from any thread. Called automatically every
        BACKUP_INTERVAL seconds; also hookable for pre-shutdown flushes.
        """
        fh = self._serial_fh
        if fh is not None:
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass  # Handle may be closed mid-call; ignore.
        try:
            os.sync()
        except AttributeError:
            pass  # Non-POSIX — nothing else we can do.

    @property
    def session_path(self) -> str:
        return str(self._session)

    @property
    def active(self) -> bool:
        return self._session_active

    @property
    def loot_root(self) -> Path:
        """Root loot directory (contains all session dirs)."""
        return self._base

    # ------------------------------------------------------------------
    # Aggregate loot database
    # ------------------------------------------------------------------

    def _load_or_build_db(self) -> dict:
        """Load loot_db.json, or rebuild from session dirs if missing/corrupt."""
        if self._db_path.is_file():
            try:
                with open(self._db_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and "version" in data and "sessions" in data:
                    log.info("Loot DB loaded: %d sessions", len(data.get("sessions", {})))
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Loot DB corrupted (%s), rebuilding", exc)
        return self._rebuild_db()

    def _rebuild_db(self) -> dict:
        """Scan all loot session directories and build the DB from scratch.

        Also generates .22000 files retroactively for any .hccapx that
        doesn't already have a corresponding .22000 file.
        """
        db: dict = {"version": 1, "sessions": {}, "totals": {}}
        if not self._base.is_dir():
            return db
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if len(name) >= 19 and name[4] == "-" and name[7] == "-" and name[10] == "_":
                self._retroactive_22000(entry)
                db["sessions"][name] = self._scan_session_dir(entry)
        self._recalc_totals(db)
        self._save_db(db)
        log.info("Loot DB rebuilt: %d sessions", len(db["sessions"]))
        return db

    def _retroactive_22000(self, session_path: Path) -> None:
        """Generate .22000 for any .hccapx without a matching .22000."""
        hs_dir = session_path / "handshakes"
        if not hs_dir.is_dir():
            return
        try:
            for f in hs_dir.iterdir():
                if f.suffix == ".hccapx" and not f.with_suffix(".22000").exists():
                    self._try_generate_22000(f)
        except OSError:
            pass

    def _scan_session_dir(self, session_path: Path) -> dict:
        """Count loot items in a single session directory."""
        counts = {"pcap": 0, "hccapx": 0, "hc22000": 0, "passwords": 0, "et_captures": 0,
                  "mc_nodes": 0, "mc_messages": 0, "bt_devices": 0, "bt_airtags": 0,
                  "bt_smarttags": 0, "bt_devices_gps": 0, "wardriving": 0, "adsb": 0,
                  "mitm_pcaps": 0}
        hs_dir = session_path / "handshakes"
        if hs_dir.is_dir():
            try:
                for f in hs_dir.iterdir():
                    if f.suffix == ".pcap":
                        counts["pcap"] += 1
                    elif f.suffix == ".hccapx":
                        counts["hccapx"] += 1
                    elif f.suffix == ".22000":
                        counts["hc22000"] += 1
            except OSError:
                pass
        pw_file = session_path / "portal_passwords.log"
        if pw_file.is_file():
            try:
                counts["passwords"] = sum(1 for _ in open(pw_file, encoding="utf-8"))
            except OSError:
                pass
        et_file = session_path / "evil_twin_capture.log"
        if et_file.is_file():
            try:
                counts["et_captures"] = sum(1 for _ in open(et_file, encoding="utf-8"))
            except OSError:
                pass
        mc_nodes_file = session_path / "meshcore_nodes.csv"
        if mc_nodes_file.is_file():
            try:
                lines = sum(1 for _ in open(mc_nodes_file, encoding="utf-8"))
                counts["mc_nodes"] = max(0, lines - 1)  # minus header
            except OSError:
                pass
        mc_msgs_file = session_path / "meshcore_messages.log"
        if mc_msgs_file.is_file():
            try:
                counts["mc_messages"] = sum(1 for _ in open(mc_msgs_file, encoding="utf-8"))
            except OSError:
                pass
        bt_dev_file = session_path / "bt_devices.csv"
        if bt_dev_file.is_file():
            try:
                gps_count = 0
                total = 0
                for i, line in enumerate(open(bt_dev_file, encoding="utf-8")):
                    if i == 0:
                        continue  # skip header
                    total += 1
                    parts = line.strip().split(",")
                    # lat,lon are last two columns
                    if len(parts) >= 8:
                        try:
                            lat = float(parts[-2])
                            lon = float(parts[-1])
                            if lat != 0.0 or lon != 0.0:
                                gps_count += 1
                        except ValueError:
                            pass
                counts["bt_devices"] = total
                counts["bt_devices_gps"] = gps_count
            except OSError:
                pass
        bt_at_file = session_path / "bt_airtag.log"
        if bt_at_file.is_file():
            try:
                total_at = 0
                total_st = 0
                for line in open(bt_at_file, encoding="utf-8"):
                    if "AirTags:" in line:
                        try:
                            part = line.split("AirTags:")[1].split("|")[0].strip()
                            total_at = max(total_at, int(part))
                        except (ValueError, IndexError):
                            pass
                    if "SmartTags:" in line:
                        try:
                            part = line.split("SmartTags:")[1].strip()
                            total_st = max(total_st, int(part))
                        except (ValueError, IndexError):
                            pass
                counts["bt_airtags"] = total_at
                counts["bt_smarttags"] = total_st
            except OSError:
                pass
        wd_file = session_path / "wardriving.csv"
        if wd_file.is_file():
            try:
                lines = sum(1 for _ in open(wd_file, encoding="utf-8"))
                counts["wardriving"] = max(0, lines - 2)  # minus pre-header + header
            except OSError:
                pass
        adsb_file = session_path / "adsb_aircraft.csv"
        if adsb_file.is_file():
            try:
                counts["adsb"] = max(0, sum(1 for _ in open(adsb_file, encoding="utf-8")) - 1)  # minus header
            except OSError:
                pass
        mitm_dir = session_path / "mitm"
        if mitm_dir.is_dir():
            try:
                counts["mitm_pcaps"] = sum(
                    1 for f in mitm_dir.iterdir() if f.suffix == ".pcap"
                )
            except OSError:
                pass
        return counts

    def _recalc_totals(self, db: dict) -> None:
        """Recalculate totals from all session entries."""
        keys = ("pcap", "hccapx", "hc22000", "passwords", "et_captures",
                "mc_nodes", "mc_messages", "bt_devices", "bt_airtags", "bt_smarttags",
                "bt_devices_gps", "wardriving", "adsb")
        totals: dict = {k: 0 for k in keys}
        totals["sessions"] = len(db["sessions"])
        for session_counts in db["sessions"].values():
            for k in keys:
                totals[k] += session_counts.get(k, 0)
        db["totals"] = totals

    def _save_db(self, db: dict) -> None:
        """Write the DB to loot_db.json atomically. fsync'd before rename so
        a power cut between tmp write and rename can't leave a zero-byte
        loot_db.json (the rename is atomic on the same filesystem)."""
        try:
            tmp = self._db_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(db, fh, indent=2)
                _fsync_file(fh)
            tmp.replace(self._db_path)
        except OSError as exc:
            log.error("Cannot save loot DB: %s", exc)

    def update_session_loot(self) -> None:
        """Rescan current session and update aggregate DB."""
        if not self._session_active:
            return
        self._db["sessions"][self._session_key] = self._scan_session_dir(self._session)
        self._recalc_totals(self._db)
        self._save_db(self._db)

    @property
    def loot_totals(self) -> dict:
        """Aggregate totals across all sessions, including cracked passwords."""
        totals = self._db.get("totals", {})
        if "cracked" not in totals:
            totals["cracked"] = self.cracked_count
        return totals

    @property
    def cracked_count(self) -> int:
        """Count of cracked passwords from WPA-sec potfile JSON."""
        if not hasattr(self, "_cracked_cache"):
            self._cracked_cache: int | None = None
        if self._cracked_cache is not None:
            return self._cracked_cache
        json_path = self._base / "passwords" / "wpasec_cracked.json"
        if json_path.is_file():
            try:
                with open(json_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._cracked_cache = data.get("count", 0)
                return self._cracked_cache
            except (json.JSONDecodeError, OSError):
                pass
        self._cracked_cache = 0
        return 0

    def invalidate_cracked_cache(self) -> None:
        """Force reload of cracked count on next access."""
        self._cracked_cache = None

    # ------------------------------------------------------------------
    # Persistent XP
    # ------------------------------------------------------------------

    def load_xp(self) -> int:
        """Load persisted XP from loot_db.json."""
        return self._db.get("xp", 0)

    def save_xp(self, xp: int) -> None:
        """Persist current XP to loot_db.json."""
        if self._db.get("xp") != xp:
            self._db["xp"] = xp
            self._save_db(self._db)

    # ------------------------------------------------------------------
    # Persistent badges
    # ------------------------------------------------------------------

    def load_badges(self) -> set[str]:
        """Load earned badges from loot_db.json."""
        return set(self._db.get("badges", []))

    def save_badge(self, badge: str) -> None:
        """Add a badge and persist to loot_db.json."""
        badges = set(self._db.get("badges", []))
        if badge not in badges:
            badges.add(badge)
            self._db["badges"] = sorted(badges)
            self._save_db(self._db)

    # ------------------------------------------------------------------
    # Persistent MeshCore contacts
    # ------------------------------------------------------------------

    def load_contacts(self) -> dict[str, dict]:
        """Load MeshCore contacts from loot_db.json.

        Returns dict keyed by node_id with fields:
        id, type, name, lat, lon, rssi, snr, last_seen, first_seen, note
        """
        return dict(self._db.get("mc_contacts", {}))

    def save_contact(self, node_id: str, data: dict) -> bool:
        """Save or update a MeshCore contact. Returns True if new contact."""
        contacts = self._db.setdefault("mc_contacts", {})
        is_new = node_id not in contacts
        if is_new:
            data.setdefault("first_seen", time.time())
            data.setdefault("note", "")
        else:
            # Preserve first_seen and note from existing
            data["first_seen"] = contacts[node_id].get("first_seen", time.time())
            data["note"] = contacts[node_id].get("note", "")
        contacts[node_id] = data
        self._save_db(self._db)
        return is_new

    def save_contact_note(self, node_id: str, note: str) -> None:
        """Update note for a MeshCore contact."""
        contacts = self._db.setdefault("mc_contacts", {})
        if node_id in contacts:
            contacts[node_id]["note"] = note
            self._save_db(self._db)

    def delete_contact(self, node_id: str) -> None:
        """Remove a MeshCore contact from loot_db."""
        contacts = self._db.get("mc_contacts", {})
        if node_id in contacts:
            del contacts[node_id]
            self._save_db(self._db)

    def calculate_xp_from_loot(self) -> int:
        """Calculate XP from all historical loot data.

        Used for initial XP bootstrap when no XP has been saved yet.
        """
        t = self._db.get("totals", {})
        xp = 0
        xp += t.get("wardriving", 0) * 15       # WiFi networks
        xp += t.get("bt_devices", 0) * 10        # BLE devices
        xp += t.get("pcap", 0) * 200             # Handshake captures
        xp += t.get("et_captures", 0) * 150      # Evil Twin credentials
        xp += t.get("passwords", 0) * 150         # Portal passwords
        xp += self.cracked_count * 150             # Cracked passwords
        return xp

    def get_known_devices(self) -> tuple[set, set]:
        """Load all known BLE MACs and WiFi BSSIDs from all loot sessions.

        Returns (known_ble_macs, known_wifi_bssids).
        """
        import csv as _csv

        ble_macs: set[str] = set()
        wifi_bssids: set[str] = set()

        if not self._base.is_dir():
            return ble_macs, wifi_bssids

        for entry in self._base.iterdir():
            if not entry.is_dir() or not entry.name[0].isdigit():
                continue

            # WiFi BSSIDs from wardriving.csv
            wd = entry / "wardriving.csv"
            if wd.is_file():
                try:
                    with open(wd, "r", encoding="utf-8", errors="replace") as f:
                        reader = _csv.reader(f)
                        for row in reader:
                            if len(row) >= 1 and ":" in row[0]:
                                wifi_bssids.add(row[0].strip().upper())
                except Exception:
                    pass

            # BLE MACs from bt_devices.csv
            bt = entry / "bt_devices.csv"
            if bt.is_file():
                try:
                    with open(bt, "r", encoding="utf-8", errors="replace") as f:
                        reader = _csv.reader(f)
                        for row in reader:
                            if len(row) >= 1 and ":" in row[0] and row[0] != "mac":
                                ble_macs.add(row[0].strip().upper())
                except Exception:
                    pass

        return ble_macs, wifi_bssids

    # ------------------------------------------------------------------
    # Full serial log
    # ------------------------------------------------------------------

    def log_serial(self, line: str) -> None:
        """Append a timestamped serial line to the full log."""
        if not self._serial_fh:
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            self._serial_fh.write(f"[{ts}] {line}\n")
            self._serial_fh.flush()
        except OSError:
            pass

        # Check for handshake metadata in serial stream
        self._detect_handshake(line)
        # Check for pcap base64 data from start_handshake_serial
        self._detect_pcap_stream(line)

    # ------------------------------------------------------------------
    # Handshake detection from serial stream
    # ------------------------------------------------------------------

    # Keywords that START handshake collection
    _HS_START_KW = ("message pair:", "message_pair:", "ANonce present")

    # Keywords that belong to a handshake block (keep collecting)
    _HS_RELATED_KW = (
        "message pair:", "message_pair:", "ANonce present", "SNonce present",
        "Key MIC", "AP MAC:", "STA MAC:", "EAPOL data:", "SSID:",
        "HANDSHAKE IS COMPLETE", "HANDSHAKE IS VALID",
        "Handshake #", "captured!", "Created ", "Failed ",
        "Cycle:", "networks captured", "attack cleanup",
        "cleanup complete", "task finished",
        "=====",  # separator lines
    )

    # Keywords that END handshake collection (save after this line)
    _HS_END_KW = ("task finished", "cleanup complete")

    def _detect_handshake(self, line: str) -> None:
        """Collect handshake metadata from serial and save when complete.

        Collects from 'message pair:' through 'task finished', saves ONCE.
        """
        stripped = line.strip()

        # Start collecting on handshake start indicators
        if not self._hs_collecting:
            if any(kw in stripped for kw in self._HS_START_KW):
                self._hs_collecting = True
                self._hs_buffer = [stripped]
            return

        # We are collecting — check if this line belongs to the block
        if any(kw in stripped for kw in self._HS_RELATED_KW) or not stripped:
            self._hs_buffer.append(stripped)
            # Check for end marker
            if any(kw in stripped for kw in self._HS_END_KW):
                self._save_handshake_buffer()
        else:
            # Unrelated line — save what we have and stop
            self._save_handshake_buffer()

    def _save_handshake_buffer(self) -> None:
        """Write collected handshake metadata to a file."""
        if not self._hs_buffer or not self._handshake_dir:
            self._hs_collecting = False
            self._hs_buffer = []
            return

        # Extract SSID and BSSID for filename
        ssid = "unknown"
        bssid = "unknown"
        for line in self._hs_buffer:
            if "SSID:" in line:
                parts = line.split("SSID:")
                if len(parts) > 1:
                    ssid = parts[1].strip().split()[0].strip(",")
            if "AP MAC:" in line:
                parts = line.split("AP MAC:")
                if len(parts) > 1:
                    bssid = parts[1].strip().replace(":", "")[:12]

        ts = datetime.now().strftime("%H%M%S")
        safe_ssid = "".join(c if c.isalnum() or c in "-_" else "_" for c in ssid)
        filename = f"{safe_ssid}_{bssid}_{ts}.txt"
        filepath = self._handshake_dir / filename

        try:
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(f"# Handshake captured at {datetime.now().isoformat()}\n")
                fh.write(f"# SSID: {ssid}\n")
                fh.write(f"# BSSID: {bssid}\n")
                fh.write(self._gps_header_lines())
                fh.write("\n")
                for line in self._hs_buffer:
                    fh.write(line + "\n")
                _fsync_file(fh)
            log.info("Handshake saved: %s", filepath)
            self._save_gps_sidecar(filepath)
            self.update_session_loot()
        except OSError as exc:
            log.error("Cannot save handshake: %s", exc)

        self._hs_collecting = False
        self._hs_buffer = []

    # ------------------------------------------------------------------
    # PCAP base64 stream parser (start_handshake_serial output)
    # ------------------------------------------------------------------

    def _detect_pcap_stream(self, line: str) -> None:
        """Parse base64-encoded pcap/hccapx blocks from serial stream.

        Triggered by start_handshake_serial firmware command which outputs:
            --- PCAP BEGIN ---
            <base64 lines>
            --- PCAP END ---
            PCAP_SIZE: <N>
            --- HCCAPX BEGIN ---
            <base64 lines>
            --- HCCAPX END ---
            SSID: <ssid>  AP: <bssid>
        """
        stripped = line.strip()

        # PCAP block
        if "--- PCAP BEGIN ---" in stripped:
            self._pcap_collecting = True
            self._pcap_b64_lines = []
            return

        if self._pcap_collecting:
            if "--- PCAP END ---" in stripped:
                self._pcap_collecting = False
            else:
                # Collect only valid base64 chars
                clean = stripped.replace(" ", "")
                if clean:
                    self._pcap_b64_lines.append(clean)
            return

        # HCCAPX block
        if "--- HCCAPX BEGIN ---" in stripped:
            self._hccapx_collecting = True
            self._hccapx_b64_lines = []
            return

        if self._hccapx_collecting:
            if "--- HCCAPX END ---" in stripped:
                self._hccapx_collecting = False
            else:
                clean = stripped.replace(" ", "")
                if clean:
                    self._hccapx_b64_lines.append(clean)
            return

        # Metadata line: firmware prints SSID and AP MAC after the blocks
        if "SSID:" in stripped and "AP:" in stripped:
            try:
                parts = stripped.split("SSID:")
                if len(parts) > 1:
                    rest = parts[1].strip()
                    if "AP:" in rest:
                        ssid_part, ap_part = rest.split("AP:", 1)
                        self._pcap_meta_ssid = ssid_part.strip()
                        self._pcap_meta_bssid = ap_part.strip().replace(":", "")[:12]
                    else:
                        self._pcap_meta_ssid = rest.strip().split()[0]
            except Exception:
                pass
            # Save when we have both pcap and hccapx (or at least pcap)
            if self._pcap_b64_lines:
                self._save_pcap_from_b64()
            return

        # Fallback: if we got hccapx end and pcap is ready, save after short wait
        # (in case firmware doesn't print SSID/AP line)
        if self._pcap_b64_lines and self._hccapx_b64_lines and not self._hccapx_collecting:
            # Check if this is an unrelated line that signals end of block
            if not any(c in stripped for c in ("BEGIN", "END", "SIZE:", "PCAP", "HCCAPX")):
                self._save_pcap_from_b64()

    def _save_pcap_from_b64(self) -> None:
        """Decode and save accumulated base64 pcap/hccapx data as binary files."""
        if not self._handshake_dir or not self._pcap_b64_lines:
            self._pcap_b64_lines = []
            self._hccapx_b64_lines = []
            self._pcap_meta_ssid = "unknown"
            self._pcap_meta_bssid = "unknown"
            return

        ts = datetime.now().strftime("%H%M%S")
        safe_ssid = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in self._pcap_meta_ssid
        )
        base_name = f"{safe_ssid}_{self._pcap_meta_bssid}_{ts}"

        # Save .pcap — fsync'd, this is unrecoverable capture data
        try:
            pcap_data = base64.b64decode("".join(self._pcap_b64_lines))
            pcap_path = self._handshake_dir / f"{base_name}.pcap"
            with open(pcap_path, "wb") as fh:
                fh.write(pcap_data)
                _fsync_file(fh)
            log.info("PCAP saved: %s (%d bytes)", pcap_path, len(pcap_data))
            self._save_gps_sidecar(pcap_path)
        except Exception as exc:
            log.error("Cannot save PCAP: %s", exc)

        # Save .hccapx (if present) — fsync'd
        if self._hccapx_b64_lines:
            try:
                hccapx_data = base64.b64decode("".join(self._hccapx_b64_lines))
                hccapx_path = self._handshake_dir / f"{base_name}.hccapx"
                with open(hccapx_path, "wb") as fh:
                    fh.write(hccapx_data)
                    _fsync_file(fh)
                log.info("HCCAPX saved: %s (%d bytes)",
                         hccapx_path, len(hccapx_data))
                self._save_gps_sidecar(hccapx_path)
                # Generate .22000 from complete handshakes
                self._try_generate_22000(hccapx_path)
            except Exception as exc:
                log.error("Cannot save HCCAPX: %s", exc)

        # Reset state and update DB
        self._pcap_b64_lines = []
        self._hccapx_b64_lines = []
        self._pcap_meta_ssid = "unknown"
        self._pcap_meta_bssid = "unknown"
        self.update_session_loot()

    # ------------------------------------------------------------------
    # GPS sidecar (Pwnagotchi-compatible .gps.json)
    # ------------------------------------------------------------------

    def _save_gps_sidecar(self, base_path: Path) -> None:
        """Write a .gps.json sidecar alongside a capture file.

        Creates e.g. MyWiFi_AABB_143022.pcap.gps.json with raw GPS fix.
        Loot always contains full (unmasked) data.
        """
        if not self._gps or not self._gps.available:
            return
        fix = self._gps.fix
        if not fix.valid:
            return
        geo_path = base_path.parent / (base_path.name + ".gps.json")
        try:
            data = {
                "Latitude": round(fix.latitude, 7),
                "Longitude": round(fix.longitude, 7),
                "Altitude": round(fix.altitude, 1),
                "Date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Satellites": fix.satellites,
                "HDOP": round(fix.hdop, 1),
            }
            with open(geo_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            log.info("GPS sidecar: %s", geo_path)
        except Exception as exc:
            log.error("Cannot save GPS sidecar: %s", exc)

    def _gps_header_lines(self) -> str:
        """Return GPS header lines for .txt handshake files, or empty string."""
        if not self._gps or not self._gps.available:
            return ""
        fix = self._gps.fix
        if not fix.valid:
            return ""
        return (
            f"# GPS: {fix.latitude:.7f}, {fix.longitude:.7f}\n"
            f"# Alt: {fix.altitude:.1f}m  Sat: {fix.satellites}  HDOP: {fix.hdop:.1f}\n"
        )

    # ------------------------------------------------------------------
    # HC22000 generation
    # ------------------------------------------------------------------

    def _try_generate_22000(self, hccapx_path: Path) -> None:
        """Generate a .22000 file from an HCCAPX if the handshake is complete."""
        from .hc22000 import convert_hccapx_to_22000

        gps_fix = None
        if self._gps and self._gps.available:
            gps_fix = self._gps.fix
        convert_hccapx_to_22000(hccapx_path, gps_fix=gps_fix)

    # ------------------------------------------------------------------
    # Scan results
    # ------------------------------------------------------------------

    # WiGLE AuthMode mapping
    _AUTH_MAP: dict[str, str] = {
        "OPEN": "[ESS]",
        "WEP": "[WEP][ESS]",
        "WPA": "[WPA-PSK-CCMP+TKIP][ESS]",
        "WPA2": "[WPA2-PSK-CCMP][ESS]",
        "WPA3": "[WPA3-SAE-CCMP][ESS]",
        "WPA/WPA2": "[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP][ESS]",
        "WPA2/WPA3": "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]",
    }

    _WIGLE_PRE_HEADER = (
        "WigleWifi-1.4,appRelease=WatchDogsGo,model=uConsole,"
        "release=1.0,device=WatchDogsGo,display=Pyxel,board=ESP32,"
        "brand=LOCOSP,star=Sol,body=3,subBody=0\n"
    )
    _WIGLE_HEADER = (
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,"
        "CurrentLatitude,CurrentLongitude,AltitudeMeters,"
        "AccuracyMeters,Type\n"
    )

    def _wigle_auth(self, auth: str) -> str:
        """Convert ESP32 auth string to WiGLE AuthMode format."""
        return self._AUTH_MAP.get(auth.strip(), f"[{auth}][ESS]")

    def save_wardriving_network(self, network: Network) -> bool:
        """Append a geo-tagged network to wardriving.csv (WiGLE format, dedup by BSSID).

        Returns True if the network was new or updated (stronger RSSI).
        """
        if not self._session_active or not network.bssid:
            return False
        path = self._session / "wardriving.csv"
        # Get GPS coords + accuracy
        lat, lon, alt, accuracy = 0.0, 0.0, 0.0, 0.0
        if self._gps and self._gps.available:
            fix = self._gps.fix
            if fix.valid:
                lat = round(fix.latitude, 7)
                lon = round(fix.longitude, 7)
                alt = round(fix.altitude, 1)
                accuracy = round(fix.hdop * 5.0, 1) if fix.hdop < 99 else 0.0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            rssi_val = int(network.rssi)
        except (ValueError, TypeError):
            rssi_val = -100
        auth_mode = self._wigle_auth(network.auth)
        # WiGLE row: MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,Lat,Lon,Alt,Accuracy,Type
        new_row = (
            f"{network.bssid},{network.ssid},{auth_mode},{ts},"
            f"{network.channel},{network.rssi},{lat},{lon},{alt},"
            f"{accuracy},WIFI\n"
        )
        # Read existing to dedup by BSSID (MAC = column 0, RSSI = column 5)
        existing: dict[str, tuple[int, int]] = {}  # bssid -> (line_index, rssi)
        lines: list[str] = []
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        lines.append(line)
                        if i <= 1:
                            continue  # pre-header + header
                        parts = line.strip().split(",")
                        if len(parts) >= 6:
                            bssid = parts[0]  # MAC column
                            try:
                                existing[bssid] = (i, int(parts[5]))
                            except (ValueError, IndexError):
                                existing[bssid] = (i, -100)
            except OSError:
                lines = []
                existing = {}
        bssid = network.bssid
        if bssid in existing:
            old_idx, old_rssi = existing[bssid]
            if rssi_val <= old_rssi:
                return False  # existing is stronger or equal
            lines[old_idx] = new_row
            try:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    fh.writelines(lines)
                    _fsync_file(fh)
            except OSError as exc:
                log.error("Cannot update wardriving CSV: %s", exc)
            return True
        else:
            try:
                if not lines:
                    with open(path, "w", newline="", encoding="utf-8") as fh:
                        fh.write(self._WIGLE_PRE_HEADER)
                        fh.write(self._WIGLE_HEADER)
                        fh.write(new_row)
                        _fsync_file(fh)
                else:
                    with open(path, "a", newline="", encoding="utf-8") as fh:
                        fh.write(new_row)
                        _fsync_file(fh)
            except OSError as exc:
                log.error("Cannot save wardriving network: %s", exc)
            return True

    def save_wardriving_bt(self, mac: str, rssi: int, name: str) -> bool:
        """Append a geo-tagged BLE device to wardriving.csv (WiGLE format, dedup by MAC).

        Uses the same CSV file as WiFi wardriving with Type=BLE.
        Returns True if the device was new or updated (stronger RSSI).
        """
        if not self._session_active or not mac:
            return False
        path = self._session / "wardriving.csv"
        # Get GPS coords + accuracy
        lat, lon, alt, accuracy = 0.0, 0.0, 0.0, 0.0
        if self._gps and self._gps.available:
            fix = self._gps.fix
            if fix.valid:
                lat = round(fix.latitude, 7)
                lon = round(fix.longitude, 7)
                alt = round(fix.altitude, 1)
                accuracy = round(fix.hdop * 5.0, 1) if fix.hdop < 99 else 0.0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # WiGLE row: MAC,SSID(=name),AuthMode,FirstSeen,Channel,RSSI,Lat,Lon,Alt,Accuracy,Type
        new_row = (
            f"{mac},{name},[BLE],{ts},"
            f",{rssi},{lat},{lon},{alt},"
            f"{accuracy},BLE\n"
        )
        # Read existing to dedup by MAC (column 0, RSSI = column 5)
        existing: dict[str, tuple[int, int]] = {}
        lines: list[str] = []
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        lines.append(line)
                        if i <= 1:
                            continue  # pre-header + header
                        parts = line.strip().split(",")
                        if len(parts) >= 6:
                            try:
                                existing[parts[0]] = (i, int(parts[5]))
                            except (ValueError, IndexError):
                                existing[parts[0]] = (i, -100)
            except OSError:
                lines = []
                existing = {}
        if mac in existing:
            old_idx, old_rssi = existing[mac]
            if rssi <= old_rssi:
                return False  # existing is stronger or equal
            lines[old_idx] = new_row
            try:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    fh.writelines(lines)
                    _fsync_file(fh)
            except OSError as exc:
                log.error("Cannot update wardriving BT CSV: %s", exc)
            return True
        else:
            try:
                if not lines:
                    with open(path, "w", newline="", encoding="utf-8") as fh:
                        fh.write(self._WIGLE_PRE_HEADER)
                        fh.write(self._WIGLE_HEADER)
                        fh.write(new_row)
                        _fsync_file(fh)
                else:
                    with open(path, "a", newline="", encoding="utf-8") as fh:
                        fh.write(new_row)
                        _fsync_file(fh)
            except OSError as exc:
                log.error("Cannot save wardriving BT device: %s", exc)
            return True

    def save_scan_results(self, networks: List[Network]) -> None:
        """Save scan results as CSV. fsync'd."""
        if not self._session_active or not networks:
            return
        filepath = self._session / "scan_results.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["#", "SSID", "BSSID", "Channel", "Auth",
                                 "RSSI", "Band", "Vendor"])
                for n in networks:
                    writer.writerow([
                        n.index, n.ssid, n.bssid, n.channel,
                        n.auth, n.rssi, n.band, n.vendor,
                    ])
                _fsync_file(fh)
            log.info("Scan results saved: %d networks", len(networks))
        except OSError as exc:
            log.error("Cannot save scan results: %s", exc)

    # ------------------------------------------------------------------
    # Sniffer results
    # ------------------------------------------------------------------

    def save_sniffer_aps(self, aps: List[SnifferAP]) -> None:
        """Save sniffer AP results as CSV. fsync'd."""
        if not self._session_active or not aps:
            return
        filepath = self._session / "sniffer_aps.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["SSID", "Channel", "Clients", "Client_MACs"])
                for ap in aps:
                    writer.writerow([
                        ap.ssid, ap.channel, ap.client_count,
                        ";".join(ap.clients),
                    ])
                _fsync_file(fh)
            log.info("Sniffer APs saved: %d", len(aps))
        except OSError as exc:
            log.error("Cannot save sniffer APs: %s", exc)

    def save_sniffer_probes(self, probes: List[ProbeEntry]) -> None:
        """Save probe requests as CSV. fsync'd."""
        if not self._session_active or not probes:
            return
        filepath = self._session / "sniffer_probes.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["SSID", "MAC"])
                for p in probes:
                    writer.writerow([p.ssid, p.mac])
                _fsync_file(fh)
            log.info("Sniffer probes saved: %d", len(probes))
        except OSError as exc:
            log.error("Cannot save sniffer probes: %s", exc)

    # ------------------------------------------------------------------
    # Portal
    # ------------------------------------------------------------------

    def save_portal_event(self, line: str) -> None:
        """Append a portal password/form submission line. fsync'd — this is
        captured-credential data we can't afford to lose on power failure."""
        if not self._session_active:
            return
        filepath = self._session / "portal_passwords.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {line}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Evil Twin
    # ------------------------------------------------------------------

    def save_evil_twin_event(self, line: str) -> None:
        """Append an evil twin capture line. fsync'd — captured credentials."""
        if not self._session_active:
            return
        filepath = self._session / "evil_twin_capture.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {line}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Attack events
    # ------------------------------------------------------------------

    def log_attack_event(self, event: str) -> None:
        """Log attack start/stop/result events. fsync'd."""
        if not self._session_active:
            return
        filepath = self._session / "attacks.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {event}\n")

    # ------------------------------------------------------------------
    # MeshCore
    # ------------------------------------------------------------------

    def save_meshcore_node(self, node_id: str, node_type: str, name: str,
                           lat: float, lon: float, rssi: float, snr: float) -> None:
        """Append node to meshcore_nodes.csv (dedup by node_id). fsync'd."""
        if not self._session_active:
            return
        path = self._session / "meshcore_nodes.csv"
        try:
            if path.is_file():
                existing = path.read_text(encoding="utf-8")
                if f",{node_id}," in existing:
                    return  # already known
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("timestamp,node_id,type,name,lat,lon,rssi,snr\n")
                    _fsync_file(fh)
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            with open(path, "a", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerow(
                    [ts, node_id, node_type, name, lat, lon, rssi, snr])
                _fsync_file(fh)
            self.update_session_loot()
        except OSError:
            pass

    def save_meshcore_message(self, channel: str, message: str, rssi: float) -> None:
        """Append message to meshcore_messages.log. fsync'd."""
        if not self._session_active:
            return
        path = self._session / "meshcore_messages.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] [{channel}] {message} (RSSI:{rssi})\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Bluetooth
    # ------------------------------------------------------------------

    def save_bt_device(self, mac: str, rssi: int, name: str,
                       is_airtag: bool, is_smarttag: bool) -> None:
        """Append BLE device to bt_devices.csv (dedup by MAC). fsync'd."""
        if not self._session_active:
            return
        path = self._session / "bt_devices.csv"
        # Get GPS coords if available
        lat, lon = 0.0, 0.0
        if self._gps and self._gps.available:
            fix = self._gps.fix
            if fix.valid:
                lat = round(fix.latitude, 7)
                lon = round(fix.longitude, 7)
        if path.is_file():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if f",{mac}," in existing or existing.startswith(f"{mac},"):
                return  # already known
        else:
            _sync_write(path,
                        "timestamp,mac,rssi,name,airtag,smarttag,lat,lon\n")
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _sync_append(
            path,
            f"{ts},{mac},{rssi},{name},{is_airtag},{is_smarttag},"
            f"{lat},{lon}\n")
        self.update_session_loot()

    def save_bt_airtag_event(self, airtags: int, smarttags: int) -> None:
        """Log an AirTag scanner detection event. fsync'd."""
        if not self._session_active:
            return
        path = self._session / "bt_airtag.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] AirTags:{airtags} | SmartTags:{smarttags}\n")
        self.update_session_loot()

    def save_adsb_aircraft(self, icao: str, callsign: str = "",
                           lat: float = 0.0, lon: float = 0.0,
                           alt_ft: int = 0, speed_kt: int = 0,
                           heading: int = 0) -> None:
        """Append an ADS-B aircraft to adsb_aircraft.csv (dedup by ICAO). fsync'd."""
        if not self._session_active or not icao:
            return
        path = self._session / "adsb_aircraft.csv"
        if path.is_file():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if f",{icao}," in existing or f"\n{icao}," in existing:
                return  # already recorded this session
        else:
            _sync_write(path, "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading\n")
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _sync_append(
            path,
            f"{ts},{icao},{callsign or ''},"
            f"{round(lat, 7)},{round(lon, 7)},"
            f"{alt_ft},{speed_kt},{heading}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # GPS point collection (for Map tab)
    # ------------------------------------------------------------------

    _gps_points_cache: list[dict] | None = None
    _gps_points_ts: float = 0.0

    def get_gps_points(self) -> list[dict]:
        """Collect all GPS-tagged loot across all sessions.

        Returns list of dicts: {lat, lon, type, label}.
        Cached for 30 seconds to avoid excessive FS scans.
        """
        now = time.monotonic()
        if self._gps_points_cache is not None and now - self._gps_points_ts < 30.0:
            return self._gps_points_cache

        points: list[dict] = []
        if not self._base.is_dir():
            self._gps_points_cache = points
            self._gps_points_ts = now
            return points

        for session_dir in self._base.iterdir():
            if not session_dir.is_dir():
                continue

            # Wardriving CSV (WiGLE format)
            wd_file = session_dir / "wardriving.csv"
            if wd_file.is_file():
                try:
                    with open(wd_file, "r", encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            if i <= 1:
                                continue  # skip pre-header + header
                            parts = line.strip().split(",")
                            if len(parts) >= 8:
                                try:
                                    lat = float(parts[6])
                                    lon = float(parts[7])
                                    if lat != 0.0 or lon != 0.0:
                                        # Detect Type column (11th col = index 10)
                                        ptype = "wifi"
                                        if len(parts) >= 11 and parts[10].strip().upper() == "BLE":
                                            ptype = "bt"
                                        points.append({
                                            "lat": lat, "lon": lon,
                                            "type": ptype,
                                            "label": parts[1],  # SSID/Name
                                            "bssid": parts[0],
                                            "auth": parts[2] if len(parts) > 2 else "",
                                            "rssi": parts[5] if len(parts) > 5 else "",
                                            "channel": parts[4] if len(parts) > 4 else "",
                                        })
                                except (ValueError, IndexError):
                                    pass
                except OSError:
                    pass

            # BT devices CSV (only WiFi + BT on map, no handshake/meshcore)
            bt_file = session_dir / "bt_devices.csv"
            if bt_file.is_file():
                try:
                    with open(bt_file, "r", encoding="utf-8") as fh:
                        reader = csv.DictReader(fh)
                        for row in reader:
                            try:
                                lat = float(row.get("lat", 0))
                                lon = float(row.get("lon", 0))
                                if lat != 0.0 or lon != 0.0:
                                    points.append({
                                        "lat": lat, "lon": lon,
                                        "type": "bt",
                                        "label": row.get("name", row.get("mac", "?")),
                                        "bssid": row.get("mac", ""),
                                        "auth": "[BLE]",
                                        "rssi": row.get("rssi", ""),
                                        "channel": "",
                                    })
                            except (ValueError, TypeError):
                                pass
                except OSError:
                    pass

        self._gps_points_cache = points
        self._gps_points_ts = now
        return points

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close file handles, write session summary, and update loot DB."""
        # Stop background sync thread before closing the handle it touches
        self._backup_stop = True
        self.flush_all()

        if self._serial_fh:
            try:
                self._serial_fh.close()
            except OSError:
                pass
            self._serial_fh = None

        # Write session summary
        if self._session_active:
            try:
                summary = self._session / "session_info.txt"
                with open(summary, "w", encoding="utf-8") as fh:
                    fh.write(f"Session ended: {datetime.now().isoformat()}\n")
                    # List files in session
                    for f in sorted(self._session.rglob("*")):
                        if f.is_file() and f.name != "session_info.txt":
                            size = f.stat().st_size
                            fh.write(f"  {f.relative_to(self._session)} "
                                     f"({size} bytes)\n")
                    _fsync_file(fh)
            except OSError:
                pass

            # Final DB update + one last OS sync so every file on the card
            # reflects our final state.
            self.update_session_loot()
            try:
                os.sync()
            except AttributeError:
                pass
