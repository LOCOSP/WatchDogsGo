"""WPA-sec integration — upload handshake pcaps, download cracked passwords."""

import json
import logging
import os
from pathlib import Path

from .config import WPASEC_URL, WPASEC_DL_URL, WPASEC_KEY

log = logging.getLogger(__name__)

# Runtime token cache (set from secrets.conf OR via in-game input dialog)
_runtime_key: str = ""


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default) or ""


def get_wpasec_key() -> str:
    """Return the active WPA-sec key (runtime > env > config)."""
    if _runtime_key:
        return _runtime_key
    # Try new env var first, then legacy, then config
    return (os.environ.get("WDG_WPASEC_KEY")
            or os.environ.get("JANOS_WPASEC_KEY")
            or WPASEC_KEY
            or "")


def set_wpasec_key(key: str) -> None:
    """Set token at runtime (from user input dialog)."""
    global _runtime_key
    _runtime_key = key.strip()


def wpasec_configured() -> bool:
    return bool(get_wpasec_key())


def save_wpasec_key(app_dir: str, key: str) -> None:
    """Persist the WPA-sec key to secrets.conf as WDG_WPASEC_KEY.
    Migrates legacy JANOS_WPASEC_KEY entries on the fly."""
    conf = Path(app_dir) / "secrets.conf"
    lines: list[str] = []
    found = False
    if conf.is_file():
        for line in conf.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("WDG_WPASEC_KEY=") or stripped.startswith("JANOS_WPASEC_KEY="):
                if not found:
                    lines.append(f"WDG_WPASEC_KEY={key}")
                    found = True
                # Drop legacy line (replaced by new one above)
            else:
                lines.append(line)
    if not found:
        if not lines:
            lines.append("# Watch Dogs Go secrets (gitignored)")
        lines.append(f"WDG_WPASEC_KEY={key}")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_wpasec_key(key)


# ── Upload ────────────────────────────────────────────────────────────────

def upload_wpasec(pcap_path: Path) -> tuple[bool, str]:
    """Upload a single .pcap to WPA-sec.  Returns (ok, message)."""
    key = get_wpasec_key()
    if not key:
        return False, "WPA-sec key not configured"
    if not pcap_path.is_file():
        return False, f"File not found: {pcap_path}"
    try:
        import requests
    except ImportError:
        return False, "requests library not installed"
    try:
        with open(pcap_path, "rb") as fh:
            files = {"file": (pcap_path.name, fh, "application/octet-stream")}
            resp = requests.post(
                WPASEC_URL,
                files=files,
                cookies={"key": key},
                timeout=60,
            )
        if resp.status_code == 200:
            body = resp.text.strip()
            if "already" in body.lower():
                return True, "Already submitted"
            return True, body[:200] if body else "Uploaded"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        log.error("WPA-sec upload error: %s", exc)
        return False, str(exc)


def _bssid_from_filename(name: str) -> str:
    """Extract BSSID from pcap filename like 'SSID_AABBCCDDEEFF_HHMMSS.pcap'.

    Returns MAC with colons (e.g. 'AA:BB:CC:DD:EE:FF') or empty string.
    """
    stem = name.rsplit(".", 1)[0]  # strip .pcap
    parts = stem.split("_")
    for part in parts:
        clean = part.replace("-", "").replace(":", "")
        if len(clean) == 12 and all(c in "0123456789ABCDEFabcdef" for c in clean):
            return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()
    return ""


def upload_wpasec_all(loot_dir: Path, blocked_macs: set[str] | None = None,
                      ) -> tuple[int, int, str]:
    """Upload all .pcap from loot/*/handshakes/.  Returns (up, total, msg).

    blocked_macs: set of uppercase MACs to skip (whitelist).
    """
    pcaps = list(loot_dir.rglob("handshakes/*.pcap"))
    if not pcaps:
        return 0, 0, "No .pcap files found"

    # Filter out whitelisted BSSIDs
    skipped = 0
    if blocked_macs:
        filtered = []
        for p in pcaps:
            bssid = _bssid_from_filename(p.name)
            if bssid and bssid in blocked_macs:
                skipped += 1
            else:
                filtered.append(p)
        pcaps = filtered

    uploaded, errors = 0, []
    for p in pcaps:
        ok, msg = upload_wpasec(p)
        if ok:
            uploaded += 1
        else:
            errors.append(f"{p.name}: {msg}")
    summary = f"{uploaded}/{len(pcaps)} uploaded"
    if skipped:
        summary += f" | {skipped} skipped (whitelist)"
    if errors:
        summary += f" | Errors: {'; '.join(errors[:3])}"
    return uploaded, len(pcaps), summary


# ── Download (potfile) ────────────────────────────────────────────────────

def download_wpasec_potfile(loot_dir: Path) -> tuple[bool, int, str]:
    """Download cracked passwords from WPA-sec.

    Returns (ok, count, message).
    Saves to loot_dir/passwords/wpasec_cracked.potfile
    """
    key = get_wpasec_key()
    if not key:
        return False, 0, "WPA-sec key not configured"
    try:
        import requests
    except ImportError:
        return False, 0, "requests library not installed"
    try:
        resp = requests.get(
            WPASEC_DL_URL,
            cookies={"key": key},
            timeout=30,
        )
        if resp.status_code != 200:
            return False, 0, f"HTTP {resp.status_code}: {resp.text[:200]}"
        body = resp.text.strip()
        if not body:
            return True, 0, "No cracked passwords yet"
        lines = [ln for ln in body.splitlines() if ln.strip()]
        pwd_dir = loot_dir / "passwords"
        pwd_dir.mkdir(parents=True, exist_ok=True)
        out = pwd_dir / "wpasec_cracked.potfile"
        out.write_text(body + "\n", encoding="utf-8")
        # Parse and save JSON cache
        parsed = parse_potfile(out)
        _save_potfile_json(loot_dir, parsed)
        return True, len(lines), f"{len(lines)} passwords saved"
    except Exception as exc:
        log.error("WPA-sec download error: %s", exc)
        return False, 0, str(exc)


# ── Potfile parsing ───────────────────────────────────────────────────────

def parse_potfile(potfile_path: Path) -> dict:
    """Parse WPA-sec potfile into structured dict.

    Format per line: AP_MAC:CLIENT_MAC:SSID:PASSWORD
    MACs are 17 chars each (xx:xx:xx:xx:xx:xx) with internal colons.

    Returns {"by_ssid": {"SSID": [{"ap_mac", "client_mac", "password"}]},
             "count": N}
    """
    result: dict = {"by_ssid": {}, "count": 0}
    if not potfile_path.is_file():
        return result
    try:
        text = potfile_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 38:
            continue
        # Char-level parsing: AP_MAC[0:17] : CLIENT_MAC[18:35] : SSID:PASSWORD
        ap_mac = line[:17]
        if line[17] != ":":
            continue
        client_mac = line[18:35]
        if line[35] != ":":
            continue
        rest = line[36:]  # SSID:PASSWORD
        if ":" not in rest:
            continue
        ssid, password = rest.rsplit(":", 1)
        if not ssid:
            continue
        result["count"] += 1
        entry = {"ap_mac": ap_mac, "client_mac": client_mac, "password": password}
        result["by_ssid"].setdefault(ssid, []).append(entry)
    return result


def _save_potfile_json(loot_dir: Path, data: dict) -> None:
    """Save parsed potfile data as JSON cache."""
    pwd_dir = loot_dir / "passwords"
    pwd_dir.mkdir(parents=True, exist_ok=True)
    out = pwd_dir / "wpasec_cracked.json"
    try:
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_wpasec_passwords(loot_dir: Path) -> dict:
    """Load parsed WPA-sec passwords from JSON cache (or parse potfile).

    Returns {"by_ssid": {...}, "count": N} or empty dict.
    """
    json_path = loot_dir / "passwords" / "wpasec_cracked.json"
    if json_path.is_file():
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "by_ssid" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: parse potfile directly
    potfile = loot_dir / "passwords" / "wpasec_cracked.potfile"
    if potfile.is_file():
        data = parse_potfile(potfile)
        _save_potfile_json(loot_dir, data)
        return data
    return {"by_ssid": {}, "count": 0}
