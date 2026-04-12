"""Convert HCCAPX binary files to hashcat .22000 (hc22000) format.

Only complete, crackable handshakes are converted.  Incomplete captures
are silently skipped so the caller can treat a ``None`` return as
"nothing useful in this file".

No external dependencies — uses only the stdlib ``struct`` module.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# -- HCCAPX binary layout (393 bytes per record) --
HCCAPX_SIGNATURE = 0x58504348  # "HCPX"
HCCAPX_RECORD_SIZE = 393
HCCAPX_FMT = "<II B B 32s B 16s 6s 32s 6s 32s H 256s"

# message_pair values that hashcat can crack (0-5 verified, +128 unverified)
_VALID_MP = frozenset(range(6)) | frozenset(range(128, 134))

_ZEROS_16 = b"\x00" * 16
_ZEROS_32 = b"\x00" * 32


# ------------------------------------------------------------------ #
# Parsing
# ------------------------------------------------------------------ #

def parse_hccapx(data: bytes) -> list[dict[str, Any]]:
    """Parse binary HCCAPX data into a list of record dicts.

    An HCCAPX file may contain multiple concatenated records (one per
    handshake captured).
    """
    records: list[dict[str, Any]] = []
    offset = 0
    while offset + HCCAPX_RECORD_SIZE <= len(data):
        fields = struct.unpack_from(HCCAPX_FMT, data, offset)
        sig, ver, mp, essid_len = fields[0], fields[1], fields[2], fields[3]
        if sig != HCCAPX_SIGNATURE:
            break
        eapol_len = fields[11]
        records.append({
            "signature": sig,
            "version": ver,
            "message_pair": mp,
            "essid_len": essid_len,
            "essid": fields[4][:essid_len],
            "keyver": fields[5],
            "keymic": fields[6],
            "mac_ap": fields[7],
            "nonce_ap": fields[8],
            "mac_sta": fields[9],
            "nonce_sta": fields[10],
            "eapol_len": eapol_len,
            "eapol": fields[12][:eapol_len],
        })
        offset += HCCAPX_RECORD_SIZE
    return records


# ------------------------------------------------------------------ #
# Validation
# ------------------------------------------------------------------ #

def is_complete(rec: dict[str, Any]) -> bool:
    """Return *True* if the record contains a crackable handshake."""
    if rec["message_pair"] not in _VALID_MP:
        return False
    if rec["essid_len"] == 0:
        return False
    if rec["eapol_len"] == 0:
        return False
    if rec["keymic"] == _ZEROS_16:
        return False
    if rec["nonce_ap"] == _ZEROS_32:
        return False
    return True


# ------------------------------------------------------------------ #
# Conversion
# ------------------------------------------------------------------ #

def record_to_22000(rec: dict[str, Any]) -> str:
    """Convert one HCCAPX record to a single .22000 hash line."""
    mic = rec["keymic"].hex()
    mac_ap = rec["mac_ap"].hex()
    mac_sta = rec["mac_sta"].hex()
    essid_hex = rec["essid"].hex()
    anonce = rec["nonce_ap"].hex()
    eapol_hex = rec["eapol"].hex()
    mp = rec["message_pair"] & 0x7F  # strip unverified flag for hash line
    return f"WPA*02*{mic}*{mac_ap}*{mac_sta}*{essid_hex}*{anonce}*{eapol_hex}*{mp:02d}"


def convert_hccapx_to_22000(
    hccapx_path: Path,
    gps_fix: Any = None,
) -> Path | None:
    """Read an HCCAPX file, validate, and write a .22000 file.

    Returns the path to the generated file, or ``None`` if no complete
    handshakes were found.
    """
    try:
        data = hccapx_path.read_bytes()
    except OSError as exc:
        log.error("Cannot read HCCAPX %s: %s", hccapx_path, exc)
        return None

    records = parse_hccapx(data)
    if not records:
        log.debug("No HCCAPX records in %s", hccapx_path.name)
        return None

    lines: list[str] = []

    # GPS comment header
    if gps_fix is not None and getattr(gps_fix, "valid", False):
        lines.append(
            f"# lat={gps_fix.latitude:.7f} "
            f"lon={gps_fix.longitude:.7f} "
            f"alt={gps_fix.altitude:.1f}"
        )

    for rec in records:
        if is_complete(rec):
            lines.append(record_to_22000(rec))

    # Only hash lines count (skip comment-only files)
    hash_lines = [ln for ln in lines if not ln.startswith("#")]
    if not hash_lines:
        log.debug("No complete handshakes in %s", hccapx_path.name)
        return None

    out_path = hccapx_path.with_suffix(".22000")
    try:
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("HC22000 saved: %s (%d hash(es))", out_path.name, len(hash_lines))
    except OSError as exc:
        log.error("Cannot write .22000: %s", exc)
        return None

    return out_path
