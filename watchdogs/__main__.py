"""Entry point: python -m watchdogs [/dev/ttyUSB0] [loot_path]"""

import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Pre-flight dependency check (runs BEFORE importing anything heavy)
# ---------------------------------------------------------------------------

def _check_deps() -> list[tuple[str, str, bool, bool]]:
    """Check all dependencies.

    Returns list of (name, status, ok, required).
    required=True  → game won't start without it
    required=False → optional (advanced attacks / LoRa), warn only
    """
    checks: list[tuple[str, str, bool, bool]] = []

    # Python version
    v = sys.version_info
    ok = v >= (3, 10)
    checks.append(("Python 3.10+", f"{v.major}.{v.minor}.{v.micro}", ok, True))

    # --- Required ---

    # pyxel
    try:
        import pyxel
        checks.append(("pyxel", pyxel.VERSION, True, True))
    except ImportError:
        checks.append(("pyxel", "NOT INSTALLED", False, True))

    # pyserial
    try:
        import serial
        checks.append(("pyserial", serial.__version__, True, True))
    except ImportError:
        checks.append(("pyserial", "NOT INSTALLED", False, True))

    # Pillow
    try:
        from PIL import Image
        import PIL
        checks.append(("Pillow", PIL.__version__, True, True))
    except ImportError:
        checks.append(("Pillow", "NOT INSTALLED", False, True))

    # --- Optional (advanced attacks) ---

    # scapy — Dragon Drain, MITM
    try:
        import scapy
        ver = getattr(scapy, "VERSION", "?")
        checks.append(("scapy", ver, True, False))
    except ImportError:
        checks.append(("scapy", "NOT INSTALLED", False, False))

    # netifaces — MITM interface detection
    try:
        import netifaces
        checks.append(("netifaces", "OK", True, False))
    except ImportError:
        checks.append(("netifaces", "NOT INSTALLED", False, False))

    # bleak — RACE Attack (BLE GATT)
    try:
        import bleak
        ver = getattr(bleak, "__version__", "OK")
        checks.append(("bleak", ver, True, False))
    except ImportError:
        checks.append(("bleak", "NOT INSTALLED", False, False))

    # dbus-python — BlueDucky (BLE HID)
    try:
        import dbus
        checks.append(("dbus-python", "OK", True, False))
    except ImportError:
        checks.append(("dbus-python", "NOT INSTALLED", False, False))

    # python3-gi — BlueZ pairing agent for PipBoy watch (new MITM NUS)
    try:
        from gi.repository import GLib  # noqa: F401
        checks.append(("python3-gi", "OK", True, False))
    except ImportError:
        checks.append(("python3-gi", "NOT INSTALLED (apt)", False, False))

    # LoRaRF — LoRa SX1262
    try:
        import LoRaRF
        checks.append(("LoRaRF", "OK", True, False))
    except ImportError:
        checks.append(("LoRaRF", "NOT INSTALLED", False, False))

    # dump1090 — ADS-B aircraft tracking (SDR)
    import shutil
    if shutil.which("dump1090"):
        checks.append(("dump1090", "OK", True, False))
    else:
        checks.append(("dump1090", "NOT INSTALLED", False, False))

    # rtl_433 — 433 MHz sensor decoding (SDR)
    if shutil.which("rtl_433"):
        checks.append(("rtl_433", "OK", True, False))
    else:
        checks.append(("rtl_433", "NOT INSTALLED", False, False))

    return checks


def _run_setup():
    """Run setup.sh to install missing dependencies."""
    project_root = Path(__file__).resolve().parent.parent
    setup_script = project_root / "setup.sh"
    if not setup_script.exists():
        print("[ERR] setup.sh not found!")
        return False
    print()
    print("=" * 50)
    print("  Running setup.sh to install dependencies...")
    print("=" * 50)
    print()
    result = subprocess.run(["bash", str(setup_script)],
                            cwd=str(project_root))
    return result.returncode == 0


def preflight():
    """Check dependencies, offer to run setup if anything missing."""
    checks = _check_deps()
    required_ok = all(ok for _, _, ok, req in checks if req)
    all_ok = all(ok for _, _, ok, _ in checks)

    if all_ok:
        return True  # All good, proceed silently

    # Something missing — show status
    print()
    print("  ESP32 Watch Dogs — Startup Check")
    print("  " + "=" * 38)
    print()

    # Required deps
    print("  Required:")
    for name, status, ok, req in checks:
        if not req:
            continue
        mark = "\033[32m OK \033[0m" if ok else "\033[31mFAIL\033[0m"
        print(f"  [{mark}] {name:20s} {status}")

    # Optional deps
    print()
    print("  Optional (advanced attacks / LoRa):")
    for name, status, ok, req in checks:
        if req:
            continue
        mark = "\033[32m OK \033[0m" if ok else "\033[33mMISS\033[0m"
        print(f"  [{mark}] {name:20s} {status}")
    print()

    if not required_ok:
        # Required deps missing — must install
        print("  Required dependencies missing.")
        print("  Running setup.sh to install them...")
        success = _run_setup()

        if success:
            # Re-check required only
            checks2 = _check_deps()
            still_bad = [n for n, _, ok, req in checks2 if req and not ok]
            if still_bad:
                print()
                print(f"  [ERR] Still missing: {', '.join(still_bad)}")
                print("  Fix manually, then try again.")
                sys.exit(1)
            print()
            print("  All required dependencies installed! Starting game...")
            print()
            return True
        else:
            print()
            print("  [ERR] setup.sh failed. Fix errors and try again.")
            sys.exit(1)
    else:
        # Required OK but optional missing — offer to install
        missing_opt = [n for n, _, ok, req in checks if not req and not ok]
        if missing_opt:
            print(f"  Optional packages missing: {', '.join(missing_opt)}")
            print("  Some attacks (MITM, Dragon Drain, BlueDucky, RACE, LoRa)")
            print("  won't work without them.")
            print()
            print("  Install now? Running setup.sh...")
            _run_setup()
            print()
            print("  Starting game...")
            print()
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _looks_like_serial(path: str) -> bool:
    """Return True if `path` looks like a serial device the game should
    open as ESP32. Accepts the obvious `/dev/tty*` and Windows `COM*`,
    plus any existing character device — covers community PTY bridges
    that emulate the projectZero serial protocol from a non-ESP32
    radio (e.g. wdg_wifi_bridge.py linking a PTY at /tmp/esp32-pty)."""
    if path.startswith("/dev/") or path.startswith("COM"):
        return True
    try:
        import stat as _stat
        return _stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def _resolve_user_home() -> tuple[str, str | None]:
    """Return (home_dir, sudo_user) — handles sudo elevation."""
    home = os.path.expanduser("~")
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd
            home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return home, sudo_user


def _diagnostic_block() -> str:
    """Build a markdown-formatted diagnostic block users can paste into
    GitHub issues. Includes everything we need to triage a bug remotely
    without asking 5 follow-up questions."""
    import platform
    info: list[tuple[str, str]] = []

    # Basics
    info.append(("Date", "<runtime>"))  # filled by logger
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        info.append(("Uptime", f"{int(up // 3600)}h {int((up % 3600) // 60)}m"))
    except Exception:
        pass

    # OS / hardware
    info.append(("OS", f"{platform.system()} {platform.release()}"))
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info.append(("Distro", line.split("=", 1)[1].strip().strip('"')))
                    break
    except Exception:
        pass
    info.append(("Arch", platform.machine()))
    info.append(("Python", platform.python_version()))

    # Hardware model (RPi/uConsole)
    try:
        with open("/sys/firmware/devicetree/base/model") as f:
            model = f.read().strip("\x00").strip()
        if model:
            info.append(("Model", model))
    except Exception:
        pass

    # Game version
    try:
        from . import __version__ as ver
        info.append(("Game version", str(ver)))
    except Exception:
        info.append(("Game version", "unknown"))

    # Detected hardware
    try:
        from .serial_manager import list_usb_serial_devices
        usb = list_usb_serial_devices()
        if usb:
            for path, desc, is_esp in usb[:3]:
                tag = " (ESP32)" if is_esp else ""
                info.append(("USB serial", f"{path}{tag} — {desc[:40]}"))
        else:
            info.append(("USB serial", "none detected"))
    except Exception as e:
        info.append(("USB serial", f"detect error: {e}"))

    # Display
    info.append(("DISPLAY", os.environ.get("DISPLAY", "<not set>")))
    info.append(("WAYLAND_DISPLAY", os.environ.get("WAYLAND_DISPLAY", "<not set>")))

    # Format as plain text (no markdown fences — the outer formatter
    # in --bugreport adds its own ``` block)
    width = max(len(k) for k, _ in info) + 1
    lines = ["=== WATCH DOGS GO — DIAGNOSTIC INFO ==="]
    for k, v in info:
        lines.append(f"  {k.ljust(width)}: {v}")
    lines.append("=======================================")
    return "\n".join(lines)


def _setup_logging():
    """Configure logging to ~/.watchdogs/last_run.log so bug reports
    have stack traces. Stdout is preserved for the boot screen.

    The log starts with a clearly-marked '=== SESSION START ===' block
    containing diagnostic info (OS, hardware, version) — users are told
    in the README to copy from this marker to the end and paste into a
    GitHub issue when reporting a bug.
    """
    import logging
    import traceback
    from datetime import datetime

    home, sudo_user = _resolve_user_home()

    log_dir = Path(home) / ".watchdogs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Make sure the dir is owned by the real user, not root
        if sudo_user:
            try:
                import pwd
                pw = pwd.getpwnam(sudo_user)
                os.chown(log_dir, pw.pw_uid, pw.pw_gid)
            except (KeyError, OSError):
                pass
    except OSError:
        return  # No log dir, no logging — fall back to stdout only

    log_path = log_dir / "last_run.log"
    prev_path = log_dir / "previous_run.log"

    # Rotate: move last_run -> previous_run on each launch
    try:
        if log_path.exists():
            log_path.replace(prev_path)
    except OSError:
        pass

    try:
        # Open append-mode handler that flushes after every write
        handler = logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        # Make sure log file is owned by the real user
        if sudo_user:
            try:
                import pwd
                pw = pwd.getpwnam(sudo_user)
                os.chown(log_path, pw.pw_uid, pw.pw_gid)
            except (KeyError, OSError):
                pass

        # Diagnostic header — copy this section to GitHub issues
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        diag = _diagnostic_block().replace("<runtime>", ts)
        # Write directly to the file so the markdown fences look right
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write("=" * 70 + "\n")
            f.write("=== SESSION START — copy from here for bug reports ===\n")
            f.write("=" * 70 + "\n")
            f.write(diag + "\n")
            f.write("\n")
        logging.info("Watch Dogs Go starting (pid=%d, user=%s)",
                     os.getpid(), sudo_user or os.environ.get("USER", "?"))
        logging.info("argv: %s", sys.argv)
    except OSError as e:
        print(f"[log] Could not open log file: {e}", file=sys.stderr)
        return

    # Catch unhandled exceptions and write them to the log before crashing
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook


def _print_bugreport():
    """Print the most recent session log formatted for pasting into a
    GitHub issue. Used by `python3 -m watchdogs --bugreport`."""
    home, _ = _resolve_user_home()
    log_path = Path(home) / ".watchdogs" / "last_run.log"
    if not log_path.exists():
        print("No log file found at", log_path)
        print("Run the game at least once to generate a log.")
        sys.exit(1)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    # Find the most recent SESSION START marker and print from there
    marker = "=== SESSION START — copy from here for bug reports ==="
    idx = text.rfind(marker)
    if idx == -1:
        print(text)
    else:
        # Include the line of equals signs above the marker
        start = text.rfind("\n", 0, idx) + 1
        # Walk back over the upper border of equals signs
        prev_line = text.rfind("\n", 0, start - 1)
        if prev_line != -1 and "=" * 30 in text[prev_line:start]:
            start = prev_line + 1
        print()
        print("# Watch Dogs Go bug report")
        print()
        print("**Describe the bug:**  <write here>")
        print()
        print("**Steps to reproduce:**  <write here>")
        print()
        print("**Diagnostic info & log:**")
        print()
        print("```")
        print(text[start:].rstrip())
        print("```")
        print()


def main():
    # Bug report mode — print the latest log session and exit
    if "--bugreport" in sys.argv or "--bug-report" in sys.argv:
        _print_bugreport()
        return

    # Set up file logging FIRST so even crashes during preflight are captured
    _setup_logging()

    # Run pre-flight checks before heavy imports
    preflight()

    # Now safe to import the game
    from .app import WatchDogsGame
    from .serial_manager import detect_esp32_port

    serial_port = None
    loot_path = None

    args = sys.argv[1:]
    for arg in args:
        if _looks_like_serial(arg):
            serial_port = arg
        else:
            loot_path = arg

    if serial_port is None:
        serial_port = detect_esp32_port()

    import logging
    logging.info("Starting game: serial=%s loot=%s",
                 serial_port, loot_path)
    try:
        WatchDogsGame(serial_port=serial_port, loot_path=loot_path)
    except Exception:
        logging.exception("Game crashed")
        raise
    finally:
        logging.info("Game exited cleanly")


if __name__ == "__main__":
    main()
