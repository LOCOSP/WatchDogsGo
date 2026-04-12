"""AIO v2 module control — direct GPIO via pinctrl.

Bypasses aiov2_ctl entirely to avoid subprocess chain issues
(pinctrl → sudo → systemctl → meshtasticd) that crash urwid.
Uses ``pinctrl set/get`` directly for instant GPIO toggling.

``aiov2_ctl`` is still used for:
- ``is_installed()`` — detect if AIO v2 hardware is present
- ``install()``      — first-time setup from GitHub
"""

import logging
import shutil
import subprocess
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

FEATURES = ("gps", "lora", "sdr", "usb")

# GPIO pin mapping from AIO v2 board (matches aiov2_ctl GPIO_MAP)
GPIO_MAP = {
    "gps":  27,
    "lora": 16,
    "sdr":  7,
    "usb":  23,
}


def _pinctrl_set(pin: int, high: bool) -> bool:
    """Set a GPIO pin via ``pinctrl set <pin> op dh|dl``."""
    state = "dh" if high else "dl"
    try:
        result = subprocess.run(
            ["pinctrl", "set", str(pin), "op", state],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("pinctrl set %s failed: %s", pin, exc)
        return False


def _pinctrl_get(pin: int) -> bool:
    """Read a GPIO pin state via ``pinctrl get <pin>``.

    Returns True if pin is HIGH (``"hi"`` in output).
    """
    try:
        result = subprocess.run(
            ["pinctrl", "get", str(pin)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return "hi" in (result.stdout or "")
    except Exception:
        return False


class AioManager:
    """Interface to HackerGadgets AIO v2 — direct GPIO control."""

    @staticmethod
    def is_installed() -> bool:
        """Check if AIO v2 is available (pinctrl present)."""
        return shutil.which("pinctrl") is not None

    @staticmethod
    def get_status() -> Optional[dict]:
        """Read all GPIO pin states directly.

        Returns dict like ``{"gps": True, "lora": False, ...}``
        or *None* if pinctrl is not available.
        """
        if not shutil.which("pinctrl"):
            return None
        try:
            status = {}
            for feat, pin in GPIO_MAP.items():
                status[feat] = _pinctrl_get(pin)
            return status
        except Exception as exc:
            log.warning("GPIO status read failed: %s", exc)
            return None

    @staticmethod
    def toggle(feature: str, on: bool) -> bool:
        """Toggle an AIO feature via direct GPIO. Returns True on success."""
        if feature not in GPIO_MAP:
            return False
        pin = GPIO_MAP[feature]
        ok = _pinctrl_set(pin, on)
        if ok:
            log.info("GPIO%d (%s) → %s", pin, feature, "HIGH" if on else "LOW")
        else:
            log.warning("GPIO%d (%s) toggle failed", pin, feature)
        return ok

    @staticmethod
    def install(callback: Callable[[str, str], None]) -> None:
        """Install aiov2_ctl from GitHub in a background thread.

        Uses the official install method from
        https://github.com/hackergadgets/aiov2_ctl/ — git clone +
        ``sudo python3 ./aiov2_ctl.py --install`` (NOT pip).

        callback(line, attr) is called for each output line.
        """
        import os
        import tempfile

        def _run():
            callback("Installing aiov2_ctl from GitHub...", "attack_active")
            try:
                tmp = tempfile.mkdtemp(prefix="aiov2_ctl_")
                clone_dir = os.path.join(tmp, "aiov2_ctl")
                callback(f"  cloning into {clone_dir}", "dim")

                # Step 1: git clone
                proc = subprocess.Popen(
                    ["git", "clone", "--depth=1",
                     "https://github.com/hackergadgets/aiov2_ctl.git",
                     clone_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        callback(f"  {line}", "dim")
                proc.wait()
                if proc.returncode != 0:
                    callback(f"git clone failed (exit {proc.returncode})", "error")
                    return

                # Step 2: sudo python3 ./aiov2_ctl.py --install
                callback("  running installer (sudo required)...", "dim")
                proc = subprocess.Popen(
                    ["sudo", "python3", "./aiov2_ctl.py", "--install"],
                    cwd=clone_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        callback(f"  {line}", "dim")
                proc.wait()
                if proc.returncode == 0:
                    callback("aiov2_ctl installed successfully!", "success")
                else:
                    callback(f"Install failed (exit code {proc.returncode})", "error")
            except FileNotFoundError as exc:
                callback(f"Install error: {exc} (is git installed?)", "error")
            except Exception as exc:
                callback(f"Install error: {exc}", "error")

        t = threading.Thread(target=_run, daemon=True)
        t.start()
