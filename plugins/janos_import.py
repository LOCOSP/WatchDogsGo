"""JanOS Loot Import — import wardriving data from JanOS into Watch Dogs Go.

JanOS is the predecessor of Watch Dogs Go. This plugin imports loot sessions
(wardriving CSV, handshakes, BT devices, serial logs) from a JanOS installation
and merges them into the game's loot directory, recalculating XP and totals.

Sessions are matched by folder name (YYYY-MM-DD_HH-MM-SS) — duplicates are
skipped, only unique JanOS sessions are imported.
"""

import json
import shutil
import threading
from pathlib import Path

from plugins.plugin_base import PluginBase, PluginMenuItem

# Common JanOS loot paths (checked in order)
_DEFAULT_PATHS = [
    Path.home() / "python" / "JanOS-app" / "janos" / "loot",
    Path.home() / "python" / "janos" / "loot",
    Path.home() / "JanOS-app" / "janos" / "loot",
    Path.home() / "JanOS" / "janos" / "loot",
    Path("/opt/janos/loot"),
]


class JanosImport(PluginBase):
    NAME = "JanOS Loot Import"
    VERSION = "1.0"
    AUTHOR = "LOCOSP"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._menu_sel = 0
        self._log: list[tuple[str, int]] = []
        self._importing = False
        self._janos_path: Path | None = None
        self._entering_path = False
        self._path_input = ""
        self._scan_result: dict | None = None
        self._auto_detect()

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("j", "JanOS Loot Import", "open_overlay"),
        ]

    def _auto_detect(self):
        """Find JanOS loot directory automatically."""
        for p in _DEFAULT_PATHS:
            if p.is_dir():
                # Verify it has session-like dirs
                has_sessions = any(
                    d.name[4:5] == "-" and d.name[10:11] == "_"
                    for d in p.iterdir() if d.is_dir()
                )
                if has_sessions:
                    self._janos_path = p
                    return

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True
        self._menu_sel = 0
        self._entering_path = False
        if self._janos_path:
            self._log_add(f"JanOS found: {self._janos_path}", 11)
        else:
            self._log_add("JanOS not found — set path manually", 10)

    def on_update(self) -> None:
        if not self._overlay_active:
            return
        import pyxel

        # Path input mode
        if self._entering_path:
            for c in "abcdefghijklmnopqrstuvwxyz0123456789.-/_":
                key_name = f"KEY_{c.upper()}" if c.isalnum() else {
                    ".": "KEY_PERIOD", "-": "KEY_MINUS", "/": "KEY_SLASH",
                    "_": "KEY_MINUS",
                }.get(c)
                if key_name and pyxel.btnp(getattr(pyxel, key_name, -1)):
                    self._path_input += c
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._path_input:
                self._path_input = self._path_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and self._path_input:
                p = Path(self._path_input).expanduser()
                if p.is_dir():
                    self._janos_path = p
                    self._log_add(f"Path set: {p}", 11)
                    self._scan_result = None
                else:
                    self._log_add(f"Not found: {p}", 8)
                self._entering_path = False
                self._path_input = ""
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._entering_path = False
                self._path_input = ""
            return

        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return

        items = self._overlay_items()
        if pyxel.btnp(pyxel.KEY_UP) and self._menu_sel > 0:
            self._menu_sel -= 1
        if pyxel.btnp(pyxel.KEY_DOWN):
            self._menu_sel = min(self._menu_sel + 1, len(items) - 1)
        if pyxel.btnp(pyxel.KEY_RETURN) and items:
            action = items[min(self._menu_sel, len(items) - 1)][0]
            self._exec(action)

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel

        pyxel.rect(0, 0, w, h, 0)

        # Title bar
        pyxel.rect(0, 0, w, 12, 1)
        path_short = str(self._janos_path)[-45:] if self._janos_path else "not set"
        pyxel.text(4, 3, f"JANOS IMPORT — {path_short}", 3)

        # Path input overlay
        if self._entering_path:
            cy = h // 2
            pyxel.rect(w // 2 - 160, cy - 30, 320, 60, 1)
            pyxel.rectb(w // 2 - 160, cy - 30, 320, 60, 10)
            pyxel.text(w // 2 - 80, cy - 20, "JANOS LOOT PATH", 10)
            display = self._path_input[-50:] + "_"
            pyxel.text(w // 2 - 100, cy, display, 7)
            pyxel.text(w // 2 - 110, cy + 16,
                       "~/python/JanOS-app/janos/loot/  [ENTER] Confirm", 13)
            return

        # Menu
        items = self._overlay_items()
        cy = 20
        for i, (action, label) in enumerate(items):
            sel = i == self._menu_sel
            c = 7 if sel else 13
            prefix = "> " if sel else "  "
            pyxel.text(4, cy, f"{prefix}{label}", c)
            cy += 10

        # Log
        lx = 4
        ly = cy + 8
        max_lines = (h - ly - 20) // 8
        for text, color in self._log[-max_lines:]:
            pyxel.text(lx, ly, text[:72], color)
            ly += 8

        pyxel.text(4, h - 12, "[ENTER] Execute  [ESC] Back", 13)

    def _overlay_items(self) -> list[tuple[str, str]]:
        items = [
            ("scan", "Scan JanOS Loot"),
            ("import_all", "Import All New Sessions"),
            ("set_path", "Set JanOS Path"),
        ]
        return items

    def _exec(self, action: str):
        if action == "scan":
            self._start_scan()
        elif action == "import_all":
            self._start_import()
        elif action == "set_path":
            self._entering_path = True
            self._path_input = str(self._janos_path or "~/python/JanOS-app/janos/loot")

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    def _start_scan(self):
        if not self._janos_path:
            self._log_add("Set JanOS path first!", 8)
            return
        self._log_add("Scanning...", 3)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        janos = self._janos_path
        if not janos or not janos.is_dir():
            self._log_add("JanOS path invalid", 8)
            return

        wdg_loot = self._wdg_loot_path()
        if not wdg_loot:
            self._log_add("Game loot path not found", 8)
            return

        existing = set()
        if wdg_loot.is_dir():
            existing = {d.name for d in wdg_loot.iterdir() if d.is_dir()}

        janos_sessions = []
        new_sessions = []
        total_wd = 0
        total_hs = 0
        total_bt = 0
        for d in sorted(janos.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            if len(name) < 19 or name[4] != "-" or name[10] != "_":
                continue
            janos_sessions.append(name)
            if name not in existing:
                new_sessions.append(name)
                # Count contents
                if (d / "wardriving.csv").is_file():
                    try:
                        lines = sum(1 for _ in open(d / "wardriving.csv")) - 2
                        total_wd += max(0, lines)
                    except OSError:
                        pass
                hs_dir = d / "handshakes"
                if hs_dir.is_dir():
                    total_hs += sum(1 for f in hs_dir.iterdir()
                                    if f.suffix in (".pcap", ".hccapx", ".22000"))
                if (d / "bt_devices.csv").is_file():
                    try:
                        lines = sum(1 for _ in open(d / "bt_devices.csv")) - 1
                        total_bt += max(0, lines)
                    except OSError:
                        pass

        self._scan_result = {
            "total": len(janos_sessions),
            "new": len(new_sessions),
            "overlap": len(janos_sessions) - len(new_sessions),
            "sessions": new_sessions,
            "wardriving": total_wd,
            "handshakes": total_hs,
            "bt_devices": total_bt,
        }

        self._log_add(f"JanOS: {len(janos_sessions)} sessions total", 13)
        self._log_add(f"  New: {len(new_sessions)} | Already in game: {len(janos_sessions) - len(new_sessions)}", 11)
        if new_sessions:
            self._log_add(f"  ~{total_wd} wardriving entries, {total_hs} handshakes, {total_bt} BT devices", 13)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def _start_import(self):
        if not self._janos_path:
            self._log_add("Set JanOS path first!", 8)
            return
        if self._importing:
            self._log_add("Import in progress...", 10)
            return
        self._importing = True
        self._log_add("Importing...", 3)
        threading.Thread(target=self._import_worker, daemon=True).start()

    def _import_worker(self):
        import time as _time
        janos = self._janos_path
        wdg_loot = self._wdg_loot_path()
        if not janos or not janos.is_dir() or not wdg_loot:
            self._log_add("Invalid paths", 8)
            self._importing = False
            return

        wdg_loot.mkdir(parents=True, exist_ok=True)
        existing = {d.name for d in wdg_loot.iterdir() if d.is_dir()}

        imported = 0
        skipped = 0
        errors = 0
        for d in sorted(janos.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            if len(name) < 19 or name[4] != "-" or name[10] != "_":
                continue
            if name in existing:
                skipped += 1
                continue

            dest = wdg_loot / name
            try:
                shutil.copytree(d, dest)
                imported += 1
                if imported % 10 == 0:
                    self._log_add(f"  {imported} sessions imported...", 13)
            except Exception as e:
                self._log_add(f"  Error {name}: {e}", 8)
                errors += 1

        self._log_add(f"Copied: {imported} new, {skipped} skipped, {errors} errors", 11)

        # Rebuild loot DB
        if imported > 0 and self.app and self.app.loot:
            self._log_add("Rebuilding loot database...", 3)
            try:
                new_db = self.app.loot._rebuild_db()
                self.app.loot._db = new_db

                # Recalc XP from totals
                t = new_db.get("totals", {})
                xp = (
                    t.get("wardriving", 0) * 15
                    + t.get("bt_devices", 0) * 10
                    + t.get("pcap", 0) * 200
                    + t.get("et_captures", 0) * 150
                    + t.get("passwords", 0) * 150
                )
                cracked = self.app.loot.cracked_count
                xp += cracked * 150
                old_xp = self.app.loot.load_xp()
                if xp > old_xp:
                    self.app.loot.save_xp(xp)
                    self.app.xp = xp
                    self.app._update_level()
                    self._log_add(f"XP: {old_xp} -> {xp} (+{xp - old_xp})", 11)

                sessions = len(new_db.get("sessions", {}))
                wd = t.get("wardriving", 0)
                hs = t.get("pcap", 0) + t.get("hccapx", 0)
                self._log_add(f"DB rebuilt: {sessions} sessions, {wd} wardriving, {hs} handshakes", 11)
                self.msg(f"[JanOS] Imported {imported} sessions (+{xp - old_xp} XP)", 11)
            except Exception as e:
                self._log_add(f"DB rebuild error: {e}", 8)
        elif imported == 0:
            self._log_add("Nothing new to import", 10)

        self._importing = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _wdg_loot_path(self) -> Path | None:
        if self.app and self.app.loot:
            return Path(self.app.loot._base)
        # Fallback
        return Path(__file__).parent.parent / "loot"

    def _log_add(self, text: str, color: int = 13):
        self._log.append((text, color))
        if len(self._log) > 50:
            self._log.pop(0)
