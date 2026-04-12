"""Privacy mode — mask sensitive data on screen for content creation.

When private mode is active, all SSIDs, MAC addresses, IP addresses,
passwords and other sensitive data are redacted in the display layer.
Loot files are NOT affected — they always contain full data.

Usage:
    from watchdogs.privacy import mask_ssid, mask_mac, mask_line, is_private
    display_text = mask_ssid(ssid)      # "eduroam" → "ed****"
    display_mac  = mask_mac(mac)        # "C4:EE:6E:5D:01:AB" → "C4:EE:**:**:**:**"
    display_line = mask_line(raw_line)   # masks MACs, IPs, passwords in free text
"""

import re
from typing import Set

_private_mode: bool = False

# Known SSIDs collected from scan results — used by mask_line()
# to mask SSIDs appearing anywhere in text (file paths, logs, etc.)
_known_ssids: Set[str] = set()


def set_private_mode(enabled: bool) -> None:
    global _private_mode
    _private_mode = enabled


def is_private() -> bool:
    return _private_mode


def register_ssids(ssids: list) -> None:
    """Register known SSIDs for line-level masking.

    Call this after scanning networks so that mask_line() can find
    and mask SSIDs anywhere in text (file paths, error messages, etc.).
    """
    global _known_ssids
    _known_ssids = set(s for s in ssids if s and len(s) > 2)


# ------------------------------------------------------------------
# Individual masking functions
# ------------------------------------------------------------------

def mask_ssid(ssid: str) -> str:
    """Mask SSID: show first 2 chars, replace rest with asterisks.

    Examples:
        "eduroam"     → "ed*****"
        "MyWiFi"      → "My****"
        "AB"          → "**"
        ""            → ""
    """
    if not _private_mode or not ssid:
        return ssid
    if len(ssid) <= 2:
        return "*" * len(ssid)
    return ssid[:2] + "*" * (len(ssid) - 2)


def mask_mac(mac: str) -> str:
    """Mask MAC address: keep first 2 octets (vendor prefix), mask rest.

    Examples:
        "C4:EE:6E:5D:01:AB" → "C4:EE:**:**:**:**"
        "0a:f1:e6:6e:5d:01" → "0a:f1:**:**:**:**"
    """
    if not _private_mode or not mac:
        return mac
    parts = mac.split(":")
    if len(parts) >= 3:
        return ":".join(parts[:2] + ["**"] * (len(parts) - 2))
    return mac


def mask_ip(ip: str) -> str:
    """Mask IP address: keep first octet, mask rest.

    Examples:
        "192.168.1.100" → "192.*.*.*"
        "10.0.0.1"      → "10.*.*.*"
    """
    if not _private_mode or not ip:
        return ip
    parts = ip.split(".")
    if len(parts) == 4:
        return parts[0] + ".*.*.*"
    return ip


def mask_password(pw: str) -> str:
    """Fully mask a password string."""
    if not _private_mode or not pw:
        return pw
    return "*" * min(8, max(len(pw), 4))


def mask_coords_str(lat: float, lon: float) -> str:
    """Format GPS coordinates for display, masked in private mode.

    Normal:  "52.229771, 21.012229"
    Private: "5x.xx, 2x.xx"
    """
    if not _private_mode:
        return f"{lat:.6f}, {lon:.6f}"
    lat_int = str(int(abs(lat)))
    lon_int = str(int(abs(lon)))
    sign_lat = "-" if lat < 0 else ""
    sign_lon = "-" if lon < 0 else ""
    return f"{sign_lat}{lat_int[0]}x.xx, {sign_lon}{lon_int[0]}x.xx"


# ------------------------------------------------------------------
# Line-level masking (for serial output / log lines)
# ------------------------------------------------------------------

# Regex patterns
_MAC_RE = re.compile(
    r'([0-9a-fA-F]{2}:[0-9a-fA-F]{2})'
    r':([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})'
)

_IP_RE = re.compile(
    r'(\d{1,3})\.\d{1,3}\.\d{1,3}\.\d{1,3}'
)

_PASSWORD_RE = re.compile(
    r'((?:Password|password|PASS|pass|pwd|PWD)\s*[:=]\s*)(.*)',
    re.IGNORECASE,
)

_SSID_RE = re.compile(
    r'((?:SSID|ssid)\s*[:=]\s*)(\S+)',
    re.IGNORECASE,
)

# MAC without colons (hex string like 336C4D or 70bc48336c4d)
_HEX_MAC_RE = re.compile(
    r'([0-9a-fA-F]{6,12})'
)

# GPS coordinates (lat, lon with 4+ decimals — avoids matching version numbers etc.)
_COORD_RE = re.compile(
    r'(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})'
)


def mask_line(line: str) -> str:
    """Apply all masking rules to a free-form text line.

    Masks MAC addresses, IP addresses, passwords, SSID references,
    and any known SSIDs found anywhere in the text (file paths, etc.).
    """
    if not _private_mode or not line:
        return line

    # Mask known SSIDs anywhere in text (file paths, error messages, etc.)
    # Do this FIRST before other regexes might alter the line
    for ssid in sorted(_known_ssids, key=len, reverse=True):
        if ssid in line:
            masked = ssid[:2] + "*" * (len(ssid) - 2)
            line = line.replace(ssid, masked)

    # Mask MAC addresses (keep first 2 octets)
    line = _MAC_RE.sub(r'\1:**:**:**:**', line)

    # Mask IP addresses (keep first octet)
    line = _IP_RE.sub(r'\1.*.*.*', line)

    # Mask passwords
    line = _PASSWORD_RE.sub(lambda m: m.group(1) + "********", line)

    # Mask SSIDs in "SSID: xxx" or "SSID=xxx" patterns
    # (catches SSIDs not in the known set)
    def _mask_ssid_match(m):
        prefix = m.group(1)
        ssid_val = m.group(2)
        if len(ssid_val) <= 2:
            return prefix + "*" * len(ssid_val)
        return prefix + ssid_val[:2] + "*" * (len(ssid_val) - 2)

    line = _SSID_RE.sub(_mask_ssid_match, line)

    # Mask GPS coordinates
    def _mask_coord(m):
        lat_s = m.group(1)
        lon_s = m.group(2)
        return lat_s[0] + "x.xx, " + lon_s[0] + "x.xx"

    line = _COORD_RE.sub(_mask_coord, line)

    return line
