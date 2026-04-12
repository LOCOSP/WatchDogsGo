"""Watch Dogs Go Wars Sync — upload wardriving loot to community server.

Uploads WiFi/BLE networks, ADS-B aircraft, and MeshCore nodes from
loot sessions to a wardrive server via HTTPS API with HMAC-SHA256
signed payloads. Supports auth push (game as 2FA authenticator).

Setup:
  1. Register on the wardrive server to get an API key
  2. Add to secrets.conf:
     WARDRIVE_API_URL=https://your-server.example/api/upload/
     WARDRIVE_API_KEY=<64-char-hex-key>
  3. Or set both from the plugin overlay (PLUGINS > Wars Sync > Set URL / Set API Key)
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from plugins.plugin_base import PluginBase, PluginMenuItem

log = logging.getLogger(__name__)

# Auth mode mapping from WiGLE CSV to simple format
# Badge ID mapping: game badge → server badge
BADGE_GAME_TO_SERVER = {
    "wardriver": "wardriver",
    "handshake_hunter": "handshake_hunter",
    "evil_twin": "evil_twin",
    "meshcore": "mesh_first",
    "wpasec_uploader": "wpa_sec_user",
    "flipper": "flipper_user",
    "skywatch": "skywatch",
    "iot_hunter": "iot_hunter",
}
# Reverse mapping: server badge → game badge
BADGE_SERVER_TO_GAME = {v: k for k, v in BADGE_GAME_TO_SERVER.items()}

AUTH_MAP = {
    "[WPA2-PSK]": "WPA2", "[WPA2]": "WPA2", "[WPA2-EAP]": "WPA2-EAP",
    "[WPA-PSK]": "WPA", "[WPA]": "WPA",
    "[WPA3-SAE]": "WPA3", "[SAE]": "WPA3",
    "[ESS]": "OPEN", "[OPEN]": "OPEN", "": "OPEN",
    "[WEP]": "WEP",
}


def _map_auth(wigle_auth: str) -> str:
    """Convert WiGLE auth string like '[WPA2-PSK]' to simple 'WPA2'."""
    if not wigle_auth:
        return "OPEN"
    result = AUTH_MAP.get(wigle_auth)
    if result:
        return result
    a = wigle_auth.upper()
    if "WPA3" in a or "SAE" in a:
        return "WPA3"
    if "WPA2" in a:
        return "WPA2"
    if "WPA" in a:
        return "WPA"
    if "WEP" in a:
        return "WEP"
    if "OPEN" in a or "ESS" in a:
        return "OPEN"
    return wigle_auth.strip("[]")


DEFAULT_API_URL = "https://wdgwars.pl/api/upload/"


def _default_endpoint() -> str:
    """Resolve default server endpoint. Override in secrets.conf with
    WARDRIVE_API_URL=... if you run your own wardrive server."""
    return DEFAULT_API_URL


def _load_secrets_conf() -> dict:
    """Load key=value pairs from secrets.conf."""
    result = {}
    path = Path(__file__).parent.parent / "secrets.conf"
    if path.is_file():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        except Exception:
            pass
    return result


class WardriveUpload(PluginBase):
    NAME = "Watch Dogs Go Wars Sync"
    VERSION = "2.1"
    AUTHOR = "LOCOSP"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._uploading = False
        self._log: list[tuple[str, int]] = []
        self._overlay_active = False
        self._menu_sel = 0
        self._uploaded_sessions: set[str] = set()
        self._api_key = ""
        self._api_url = ""
        self._key_input = ""
        self._url_input = ""
        self._entering_key = False
        self._entering_url = False
        # Auth push — PIN display
        self._auth_pin = ""
        self._auth_pin_expiry = 0
        self._auth_dismissed_token = ""
        self._auth_current_token = ""
        self._auth_polling = False
        self._auth_poll_thread = None
        # Identity from /api/me
        self._username = ""
        self._user_gang = ""
        self._user_stats: dict = {}
        self._load_state()

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("w", "Watch Dogs Go Wars Sync", "open_overlay"),
        ]

    def on_load(self, app) -> None:
        super().on_load(app)
        self._start_auth_poll()
        # If a key is already configured, fetch identity in background
        # so badges sync and LoRa name updates without user interaction.
        if self._api_key and self._api_url:
            threading.Thread(
                target=self._validate_and_apply_user,
                daemon=True).start()

    def on_unload(self) -> None:
        self._auth_polling = False

    def _check_level(self) -> bool:
        """Check if player meets level requirement. Returns True if OK."""
        if not self.app:
            return True
        if self.app.level < 6:
            lvl = self.app.level
            title = self.app.level_title
            self.msg(f"[Sync] Locked — reach Lv.6 WARDRIVER to unlock (Lv.{lvl} {title})", 10)
            self.msg("[Sync] Earn XP: scan WiFi/BLE, capture handshakes, wardrive!", 13)
            return False
        return True

    def open_overlay(self):
        if not self._check_level():
            return
        self._overlay_active = True
        self._menu_sel = 0
        self._entering_key = False
        self._entering_url = False
        if not self._api_key:
            self._log_add("Set your API key to start syncing", 10)
            self._log_add("  Register on the community server to get your key", 13)
        else:
            self._log_add("Connected to server", 11)
        n = len(self._pending_sessions())
        if n:
            self._log_add(f"{n} session(s) with new data", 11)
        else:
            self._log_add("No new wardriving data", 13)

    def on_update(self) -> None:
        if not self._overlay_active:
            return
        import pyxel

        # Key input mode
        if self._entering_key:
            for c in "0123456789abcdef":
                if pyxel.btnp(getattr(pyxel, f"KEY_{c.upper()}")):
                    self._key_input += c
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._key_input:
                self._key_input = self._key_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and len(self._key_input) >= 16:
                self._api_key = self._key_input
                self._save_secret("WARDRIVE_API_KEY", self._api_key)
                self._log_add(f"API key saved ({len(self._api_key)} chars)", 11)
                self._entering_key = False
                self._key_input = ""
                self._start_auth_poll()
                # Validate the new key against /api/me and pull identity/badges
                self._log_add("Validating key with server...", 3)
                threading.Thread(
                    target=self._validate_and_apply_user,
                    daemon=True).start()
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._entering_key = False
                self._key_input = ""
            return

        # URL input mode
        if self._entering_url:
            for c in "abcdefghijklmnopqrstuvwxyz0123456789.-/:_":
                key = f"KEY_{c.upper()}" if c.isalnum() else {
                    ".": "KEY_PERIOD", "-": "KEY_MINUS", "/": "KEY_SLASH",
                    ":": "KEY_SEMICOLON", "_": "KEY_MINUS",
                }.get(c)
                if key and pyxel.btnp(getattr(pyxel, key, -1)):
                    self._url_input += c
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._url_input:
                self._url_input = self._url_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and self._url_input.startswith("http"):
                self._api_url = self._url_input
                if not self._api_url.endswith("/"):
                    self._api_url += "/"
                self._save_secret("WARDRIVE_API_URL", self._api_url)
                self._log_add(f"Server URL saved", 11)
                self._entering_url = False
                self._url_input = ""
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._entering_url = False
                self._url_input = ""
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
        server_short = self._api_url.replace("https://", "").replace("http://", "").rstrip("/") if self._api_url else "no server"
        pyxel.text(4, 3, f"WARS SYNC — {server_short[:40]}", 3)
        key_txt = f"KEY:{self._api_key[:8]}..." if self._api_key else "NO KEY"
        key_c = 11 if self._api_key else 8
        pyxel.text(w - 80, 3, key_txt, key_c)

        # Key input overlay
        if self._entering_key:
            cy = h // 2
            pyxel.rect(w // 2 - 140, cy - 30, 280, 60, 1)
            pyxel.rectb(w // 2 - 140, cy - 30, 280, 60, 10)
            pyxel.text(w // 2 - 80, cy - 20, "ENTER API KEY (hex)", 10)
            display = self._key_input[-40:] + "_"
            pyxel.text(w // 2 - 80, cy, display, 7)
            pyxel.text(w // 2 - 90, cy + 16,
                       f"[0-9a-f] Type  [ENTER] Confirm  ({len(self._key_input)} chars)", 13)
            return

        # URL input overlay
        if self._entering_url:
            cy = h // 2
            pyxel.rect(w // 2 - 160, cy - 30, 320, 60, 1)
            pyxel.rectb(w // 2 - 160, cy - 30, 320, 60, 10)
            pyxel.text(w // 2 - 80, cy - 20, "ENTER SERVER URL", 10)
            display = self._url_input[-50:] + "_"
            pyxel.text(w // 2 - 100, cy, display, 7)
            pyxel.text(w // 2 - 110, cy + 16,
                       "https://your-server.example/api/upload/  [ENTER] Confirm", 13)
            return

        # Left: menu
        items = self._overlay_items()
        cy = 20
        for i, (action, label) in enumerate(items):
            sel = i == self._menu_sel
            c = 7 if sel else 13
            prefix = "\x10" if sel else " "
            pyxel.text(4, cy, f"{prefix} {label}", c)
            cy += 10

        # Right: log
        lx = 250
        pyxel.line(lx - 4, 14, lx - 4, h - 16, 1)
        pyxel.text(lx, 16, "-- Sync Log --", 3)
        ly = 28
        max_lines = (h - 44) // 8
        for text, color in self._log[-max_lines:]:
            pyxel.text(lx, ly, text[:48], color)
            ly += 8

        pyxel.text(4, h - 22,
                   f"Uploaded: {len(self._uploaded_sessions)} sessions  |  "
                   f"Requires: Lv.6 WARDRIVER (6000 XP)", 13)
        pyxel.text(4, h - 12, "[ENTER] Execute  [ESC] Back", 13)

        # Auth PIN popup (drawn on top of everything)
        if self._auth_pin_active():
            self._draw_auth_pin(w, h)

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    # ------------------------------------------------------------------
    def _overlay_items(self) -> list[tuple[str, str]]:
        n = len(self._pending_sessions())
        items = [
            ("upload_all", f"Upload All ({n} pending)"),
            ("upload_latest", "Upload Latest Session"),
            ("show_profile", "Show My Profile"),
            ("sync_badges", "Sync Badges"),
            ("set_key", "Set API Key"),
            ("test_api", "Test API Connection"),
            ("show_pending", "Show Pending Sessions"),
            ("reset", "Reset Upload History"),
        ]
        return items

    def _exec(self, action: str):
        if action == "upload_all":
            self._start_upload(all_sessions=True)
        elif action == "upload_latest":
            self._start_upload(all_sessions=False)
        elif action == "set_url":
            self._entering_url = True
            self._url_input = self._api_url
        elif action == "set_key":
            self._entering_key = True
            self._key_input = self._api_key
        elif action == "show_profile":
            self._show_profile()
        elif action == "sync_badges":
            self._start_badge_sync()
        elif action == "test_api":
            self._test_api()
        elif action == "show_pending":
            for s in self._pending_sessions():
                self._log_add(f"  {s.name}", 13)
        elif action == "reset":
            self._uploaded_sessions.clear()
            self._save_state()
            self._log_add("Upload history cleared", 10)

    # ------------------------------------------------------------------
    # Upload via API
    # ------------------------------------------------------------------
    def _pending_sessions(self) -> list[Path]:
        if not self.app or not self.app.loot:
            return []
        base = Path(self.app.loot._base)
        pending = []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            # Session has data if any of these exist
            has_data = any((d / f).is_file() for f in
                          ["wardriving.csv", "adsb_aircraft.csv", "meshcore_nodes.csv"])
            if has_data and d.name not in self._uploaded_sessions:
                pending.append(d)
        return pending

    def _parse_csv(self, csv_path: Path) -> list[dict]:
        """Parse WiGLE CSV to list of network dicts."""
        if not csv_path.is_file():
            return []
        networks = []
        try:
            import csv as _csv
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                # Skip WiGLE pre-header line
                first = f.readline()
                if not first:
                    return []
                reader = _csv.reader(f)
                header = next(reader, None)
                if not header:
                    return []
                for parts in reader:
                    if len(parts) < 11:
                        continue
                    try:
                        lat = float(parts[6]) if parts[6] else 0.0
                        lon = float(parts[7]) if parts[7] else 0.0
                    except ValueError:
                        continue
                    if lat == 0.0 and lon == 0.0:
                        continue
                    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        continue
                    networks.append({
                        "bssid": parts[0],
                        "ssid": parts[1],
                        "auth": _map_auth(parts[2]),
                        "first_seen": parts[3],
                        "channel": int(parts[4]) if parts[4].isdigit() else 0,
                        "rssi": int(parts[5]) if parts[5].lstrip("-").isdigit() else -100,
                        "lat": lat,
                        "lon": lon,
                        "alt": float(parts[8]) if parts[8] else 0.0,
                        "type": parts[10] if len(parts) > 10 else "WIFI",
                    })
        except Exception as e:
            self._log_add(f"CSV parse error: {e}", 8)
        return networks

    def _parse_adsb(self, csv_path: Path) -> list[dict]:
        """Parse adsb_aircraft.csv to list of aircraft dicts."""
        if not csv_path.is_file():
            return []
        aircraft = {}
        try:
            import csv as _csv
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                reader = _csv.reader(f)
                header = next(reader, None)
                if not header:
                    return []
                for parts in reader:
                    if len(parts) < 8:
                        continue
                    try:
                        lat = float(parts[3]) if parts[3] else 0.0
                        lon = float(parts[4]) if parts[4] else 0.0
                    except ValueError:
                        continue
                    if lat == 0.0 and lon == 0.0:
                        continue
                    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        continue
                    icao = parts[1]
                    if not icao or icao in aircraft:
                        continue
                    try:
                        alt = int(float(parts[5])) if parts[5] else 0
                        spd = int(float(parts[6])) if parts[6] else 0
                        hdg = int(float(parts[7])) if parts[7] else 0
                    except ValueError:
                        alt = spd = hdg = 0
                    aircraft[icao] = {
                        "icao": icao,
                        "callsign": parts[2],
                        "lat": lat,
                        "lon": lon,
                        "alt_ft": alt,
                        "speed_kt": spd,
                        "heading": hdg,
                        "first_seen": parts[0],
                        "type": "ADSB",
                    }
        except Exception as e:
            self._log_add(f"ADS-B parse error: {e}", 8)
        return list(aircraft.values())

    def _parse_meshcore(self, csv_path: Path) -> list[dict]:
        """Parse meshcore_nodes.csv to list of node dicts."""
        if not csv_path.is_file():
            return []
        nodes = {}
        try:
            import csv as _csv
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                reader = _csv.reader(f)
                header = next(reader, None)
                if not header:
                    return []
                for parts in reader:
                    if len(parts) < 6:
                        continue
                    try:
                        lat = float(parts[4]) if parts[4] else 0.0
                        lon = float(parts[5]) if parts[5] else 0.0
                    except ValueError:
                        continue
                    if lat == 0.0 and lon == 0.0:
                        continue
                    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                        continue
                    node_id = parts[1]
                    if not node_id or node_id in nodes:
                        continue
                    try:
                        rssi = float(parts[6]) if len(parts) > 6 and parts[6] else 0
                    except ValueError:
                        rssi = 0
                    nodes[node_id] = {
                        "node_id": node_id,
                        "node_type": parts[2],
                        "name": parts[3],
                        "lat": lat,
                        "lon": lon,
                        "rssi": rssi,
                        "first_seen": parts[0],
                        "type": "MESHCORE",
                    }
        except Exception as e:
            self._log_add(f"MeshCore parse error: {e}", 8)
        return list(nodes.values())

    def _start_upload(self, all_sessions: bool = True):
        if not self._api_url:
            self._log_add("Set server URL first!", 8)
            return
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        if self._uploading:
            self._log_add("Upload in progress...", 10)
            return
        sessions = self._pending_sessions()
        if not all_sessions and sessions:
            sessions = sessions[-1:]
        if not sessions:
            self._log_add("Nothing to upload", 10)
            return
        self._uploading = True
        threading.Thread(
            target=self._upload_worker, args=(sessions,),
            daemon=True).start()

    def _upload_worker(self, sessions: list[Path]):
        import time as _time
        total = len(sessions)
        uploaded = 0
        total_nets = 0
        for i, session_dir in enumerate(sessions):
            networks = self._parse_csv(session_dir / "wardriving.csv")
            aircraft = self._parse_adsb(session_dir / "adsb_aircraft.csv")
            mc_nodes = self._parse_meshcore(session_dir / "meshcore_nodes.csv")

            if not networks and not aircraft and not mc_nodes:
                self._log_add(f"[{i+1}/{total}] {session_dir.name}: no GPS data", 13)
                self._uploaded_sessions.add(session_dir.name)
                continue

            parts = []
            if networks:
                parts.append(f"{len(networks)} nets")
            if aircraft:
                parts.append(f"{len(aircraft)} aircraft")
            if mc_nodes:
                parts.append(f"{len(mc_nodes)} mesh")
            self._log_add(
                f"[{i+1}/{total}] {session_dir.name}: {', '.join(parts)}", 3)

            try:
                payload = self._sign_payload({
                    "networks": networks,
                    "aircraft": aircraft,
                    "meshcore_nodes": mc_nodes,
                })
                req = Request(self._api_url, data=payload, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("X-API-Key", self._api_key)
                with urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode())
                    imp = result.get("imported", 0)
                    dup = result.get("duplicates", 0)
                    ac_i = result.get("aircraft_imported", 0)
                    mc_i = result.get("meshcore_imported", 0)
                    info = f"+{imp} nets"
                    if ac_i:
                        info += f", +{ac_i} ac"
                    if mc_i:
                        info += f", +{mc_i} mesh"
                    if dup:
                        info += f", {dup} dup"
                    self._log_add(f"  OK: {info}", 11)
                    self._handle_upload_badges(result)
                    total_nets += imp
                    uploaded += 1
                    self._uploaded_sessions.add(session_dir.name)
                    self._save_state()
            except HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:60]
                if e.code == 429:
                    self._log_add("  Rate limit — retrying in 5s...", 10)
                    _time.sleep(5)
                    try:
                        payload = self._sign_payload({
                            "networks": networks,
                            "aircraft": aircraft,
                            "meshcore_nodes": mc_nodes,
                        })
                        req = Request(self._api_url, data=payload, method="POST")
                        req.add_header("Content-Type", "application/json")
                        req.add_header("X-API-Key", self._api_key)
                        with urlopen(req, timeout=30) as resp:
                            result = json.loads(resp.read().decode())
                            imp = result.get("imported", 0)
                            self._log_add(f"  Retry OK: +{imp}", 11)
                            self._handle_upload_badges(result)
                            total_nets += imp
                            uploaded += 1
                            self._uploaded_sessions.add(session_dir.name)
                            self._save_state()
                    except Exception as e2:
                        self._log_add(f"  Retry failed: {e2}", 8)
                else:
                    self._log_add(f"  HTTP {e.code}: {body}", 8)
            except URLError as e:
                self._log_add(f"  Connection error: {e.reason}", 8)
            except Exception as e:
                self._log_add(f"  Error: {e}", 8)

            if i < total - 1:
                _time.sleep(1)

        self._log_add(
            f"Done: {uploaded}/{total} sessions, +{total_nets} networks",
            11 if uploaded == total else 10)
        self._uploading = False
        # Auto-sync badges after upload
        if uploaded > 0:
            self._badge_sync_worker()

    def _sign_payload(self, data: dict) -> bytes:
        """Sign payload with HMAC-SHA256. Server verifies before processing."""
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_hex(8)
        data_b64 = base64.b64encode(raw).decode("ascii")
        sig = hmac.new(
            self._api_key.encode("utf-8"),
            (nonce + data_b64).encode("utf-8"),
            hashlib.sha256).hexdigest()
        return json.dumps({
            "data": data_b64, "nonce": nonce, "sig": sig,
        }).encode("utf-8")

    # ------------------------------------------------------------------
    # Badge sync
    # ------------------------------------------------------------------
    def _get_game_badges(self) -> list[str]:
        """Get current game badges mapped to server IDs."""
        if not self.app:
            return []
        server_badges = []
        for game_id in self.app._badges:
            srv = BADGE_GAME_TO_SERVER.get(game_id)
            if srv:
                server_badges.append(srv)
        return server_badges

    def _apply_server_badges(self, server_badges: list[str]) -> list[str]:
        """Import server badges into game. Returns list of newly earned game badges."""
        if not self.app:
            return []
        new = []
        for srv_id in server_badges:
            game_id = BADGE_SERVER_TO_GAME.get(srv_id)
            if game_id and game_id not in self.app._badges:
                self.app._earn_badge(game_id)
                new.append(game_id)
        return new

    def _start_badge_sync(self):
        if not self._api_url or not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Syncing badges...", 3)
        threading.Thread(target=self._badge_sync_worker, daemon=True).start()

    def _badge_sync_worker(self):
        try:
            api_base = self._api_url.rsplit("/api/", 1)[0]
            url = f"{api_base}/api/badges/"
            game_badges = self._get_game_badges()
            payload = json.dumps({"badges": game_badges}).encode()
            req = Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-API-Key", self._api_key)
            with urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            all_badges = result.get("badges", [])
            game_imported = result.get("game_imported", 0)
            server_awarded = result.get("server_awarded", [])
            new_game = self._apply_server_badges(all_badges)
            parts = []
            if game_imported:
                parts.append(f"{game_imported} pushed")
            if server_awarded:
                parts.append(f"{len(server_awarded)} from server")
            if new_game:
                parts.append(f"{len(new_game)} new in game")
                for b in new_game:
                    self.msg(f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)
            total = len(all_badges)
            info = ", ".join(parts) if parts else "in sync"
            self._log_add(f"Badges: {total} total — {info}", 11)
        except HTTPError as e:
            self._log_add(f"Badge sync: HTTP {e.code}", 8)
        except Exception as e:
            self._log_add(f"Badge sync: {e}", 8)

    def _handle_upload_badges(self, result: dict):
        """Process new_badges from upload response."""
        new_badges = result.get("new_badges", [])
        if not new_badges:
            return
        new_game = self._apply_server_badges(new_badges)
        for b in new_game:
            self.msg(f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)
        if new_game:
            self._log_add(f"  +{len(new_game)} badge(s) from server!", 11)

    # ------------------------------------------------------------------
    # /api/me — fetch user identity, stats and badges from server
    # ------------------------------------------------------------------
    def _api_me_url(self) -> str:
        if not self._api_url:
            return ""
        api_base = self._api_url.rsplit("/api/", 1)[0]
        return f"{api_base}/api/me"

    def _validate_and_apply_user(self):
        """Background worker — fetch /api/me after key save and apply identity."""
        info = self.fetch_user_info()
        if info:
            self._on_user_info(info, sync_lora_name=True)

    def _show_profile(self):
        """Menu action — refresh /api/me on demand and show stats in log."""
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Fetching profile from server...", 3)
        threading.Thread(
            target=lambda: self._on_user_info(
                self.fetch_user_info() or {}, sync_lora_name=False),
            daemon=True).start()

    def fetch_user_info(self) -> dict | None:
        """GET /api/me — returns dict with username, stats, badges, gang.
        Returns None on any error. Logs status to plugin log."""
        if not self._api_url or not self._api_key:
            return None
        try:
            req = Request(self._api_me_url())
            req.add_header("X-API-Key", self._api_key)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("ok"):
                err = data.get("error", "unknown error")
                self._log_add(f"  /api/me: {err}", 8)
                return None
            return data
        except HTTPError as e:
            if e.code == 401:
                self._log_add("  Invalid API key (401)", 8)
            else:
                self._log_add(f"  /api/me: HTTP {e.code}", 8)
            return None
        except URLError as e:
            self._log_add(f"  /api/me: {e.reason}", 8)
            return None
        except Exception as e:
            self._log_add(f"  /api/me: {e}", 8)
            return None

    def _on_user_info(self, info: dict, *, sync_lora_name: bool = True):
        """Apply user info from /api/me — show stats, sync badges, update LoRa name."""
        if not info or not info.get("username"):
            return  # fetch failed; error already logged
        username = info.get("username", "")
        gang = info.get("gang") or "(no gang)"
        wifi = info.get("wifi", 0)
        ble = info.get("ble", 0)
        ac = info.get("aircraft", 0)
        mesh = info.get("mesh", 0)
        cracked = info.get("cracked", 0)
        total = info.get("total", 0)
        badges = info.get("badges", [])

        self._username = username
        self._user_gang = gang
        self._user_stats = {
            "wifi": wifi, "ble": ble, "aircraft": ac, "mesh": mesh,
            "cracked": cracked, "total": total,
        }

        self._log_add(f"Welcome, {username}!", 11)
        self._log_add(f"  Gang: {gang}", 13)
        self._log_add(f"  Stats: {wifi} wifi, {ble} ble, {ac} ac, {mesh} mesh", 13)
        if cracked:
            self._log_add(f"  Cracked passwords: {cracked}", 13)
        self._log_add(f"  Total records on server: {total}", 13)
        self._log_add(f"  Badges on server: {len(badges)}", 13)

        # Sync badges from /api/me payload
        if badges and self.app:
            new_game = self._apply_server_badges(badges)
            if new_game:
                self._log_add(f"  +{len(new_game)} badge(s) imported from server", 11)
                for b in new_game:
                    self.msg(
                        f"[BADGE] Unlocked: {b.upper().replace('_', ' ')}", 11)

        # Sync LoRa node name to portal username (with WDG_ prefix for clarity)
        if sync_lora_name and username and self.app:
            target_name = f"WDG_{username}"
            current = getattr(self.app, "_mc_node_name", "")
            # Only auto-update if user still has the auto-generated name
            # (starts with "WatchDogs_") or matches an old WDG_ prefix
            if current.startswith("WatchDogs_") or current.startswith("WDG_"):
                if current != target_name:
                    self.app._mc_node_name = target_name
                    try:
                        from watchdogs.lora_manager import save_meshcore_config
                        save_meshcore_config(
                            target_name,
                            getattr(self.app, "_mc_channels_list", []))
                        self._log_add(
                            f"  LoRa node name -> {target_name}", 11)
                        self.msg(
                            f"[LoRa] Identity synced: {target_name}", 11)
                    except Exception as e:
                        self._log_add(f"  LoRa name save error: {e}", 8)
            else:
                # User picked a custom name — don't override silently
                self._log_add(
                    f"  LoRa node kept as '{current}' (custom)", 13)

    # ------------------------------------------------------------------
    # API test
    # ------------------------------------------------------------------
    def _test_api(self):
        if not self._api_url:
            self._log_add("Set server URL first!", 8)
            return
        if not self._api_key:
            self._log_add("Set API key first!", 8)
            return
        self._log_add("Testing API...", 3)
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        try:
            payload = self._sign_payload({"networks": []})
            req = Request(self._api_url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-API-Key", self._api_key)
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                total = result.get("total", "?")
                self._log_add(f"API OK — {total} records on server", 11)
        except HTTPError as e:
            if e.code == 401:
                self._log_add("API: Invalid key (401)", 8)
            elif e.code == 403:
                self._log_add("API: Invalid signature (403)", 8)
            else:
                self._log_add(f"API: HTTP {e.code}", 8)
        except URLError as e:
            self._log_add(f"API: {e.reason}", 8)
        except Exception as e:
            self._log_add(f"API: {e}", 8)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _state_file(self) -> Path:
        return Path(__file__).parent / ".wardrive_state.json"

    def _load_state(self):
        # Load uploaded sessions
        p = self._state_file()
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                self._uploaded_sessions = set(data.get("uploaded", []))
            except Exception:
                pass
        # Load config from secrets.conf
        conf = _load_secrets_conf()
        self._api_key = conf.get("WARDRIVE_API_KEY", "")
        self._api_url = conf.get("WARDRIVE_API_URL", "") or _default_endpoint()

    def _save_state(self):
        try:
            self._state_file().write_text(json.dumps({
                "uploaded": sorted(self._uploaded_sessions),
            }))
        except Exception:
            pass

    def _save_secret(self, key: str, value: str):
        """Save a key=value to secrets.conf."""
        secrets_path = Path(__file__).parent.parent / "secrets.conf"
        lines = []
        found = False
        if secrets_path.is_file():
            lines = secrets_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        secrets_path.write_text("\n".join(lines) + "\n")

    def _log_add(self, text: str, color: int = 13):
        self._log.append((text, color))
        if len(self._log) > 100:
            self._log = self._log[-100:]
        if self.app:
            self.app._term_add(f"[Sync] {text}", raw=True)

    # ------------------------------------------------------------------
    # Auth push — game as authenticator
    # ------------------------------------------------------------------
    def _start_auth_poll(self):
        """Start background polling for auth requests from web."""
        if self._auth_polling or not self._api_key or not self._api_url:
            return
        self._auth_polling = True
        self._auth_poll_thread = threading.Thread(
            target=self._auth_poll_worker, daemon=True)
        self._auth_poll_thread.start()

    def _auth_poll_worker(self):
        """Poll server for pending auth requests every 5 seconds."""
        import time as _time
        api_base = self._api_url.rsplit("/api/", 1)[0]
        poll_url = f"{api_base}/api/auth/pending/"

        while self._auth_polling:
            try:
                req = Request(poll_url)
                req.add_header("X-API-Key", self._api_key)
                with urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    if data.get("pending"):
                        token = data.get("token", "")
                        if token == self._auth_dismissed_token:
                            continue
                        pin = data["pin"]
                        expires = data.get("expires", 60)
                        self._auth_pin = str(pin)
                        self._auth_pin_expiry = _time.time() + min(expires, 20)
                        self._auth_current_token = token
                        self._log_add(f"LOGIN PIN: {pin}", 10)
                        if self.app:
                            self.app.msg(f"[AUTH] Login PIN: {pin}", 10)
            except Exception:
                pass
            _time.sleep(5)

    def _auth_pin_active(self) -> bool:
        """Check if there's an active PIN to display."""
        import time as _time
        if self._auth_pin and _time.time() < self._auth_pin_expiry:
            return True
        if self._auth_pin:
            self._auth_pin = ""
        return False

    def _draw_auth_pin(self, w: int = 640, h: int = 360):
        """Draw large PIN popup centered on screen."""
        import pyxel
        import time as _time

        remaining = int(self._auth_pin_expiry - _time.time())
        cx, cy = w // 2, h // 2
        bw, bh = 280, 90
        x0 = cx - bw // 2
        y0 = cy - bh // 2
        bf = self.app._big_font if self.app else None
        # Background
        pyxel.rect(x0, y0, bw, bh, 0)
        pyxel.rectb(x0, y0, bw, bh, 10)
        pyxel.rectb(x0 + 2, y0 + 2, bw - 4, bh - 4, 3)
        # Title
        pyxel.text(cx - 40, y0 + 6, "WEB LOGIN REQUEST", 10)
        pyxel.line(x0 + 8, y0 + 16, x0 + bw - 8, y0 + 16, 3)
        # PIN digits in big font
        pin_str = self._auth_pin
        if bf:
            char_w = 12
            total = len(pin_str) * char_w
            px = cx - total // 2
            for ch in pin_str:
                pyxel.text(px, y0 + 26, ch, 7, bf)
                px += char_w
        else:
            px = cx - len(pin_str) * 6
            for ch in pin_str:
                pyxel.text(px, y0 + 28, ch, 7)
                px += 12
        # Progress bar
        bar_w = bw - 40
        bar_x = x0 + 20
        bar_y = y0 + bh - 24
        pyxel.rect(bar_x, bar_y, bar_w, 4, 1)
        fill = max(0, int(bar_w * remaining / 20))
        tc = 11 if remaining > 10 else (10 if remaining > 5 else 8)
        pyxel.rect(bar_x, bar_y, fill, 4, tc)
        # Hint
        pyxel.text(cx - 55, y0 + bh - 14, f"Expires in {remaining}s  [ESC] dismiss", 13)
