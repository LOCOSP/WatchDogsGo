"""Flipper Zero serial CLI manager.

Handles serial communication with Flipper Zero over USB CDC.
Supports SubGHz RX/TX, NFC, device info, storage access.
Auto-detects Flipper by USB VID:PID (0483:5740).
"""

import logging
import os
import re
import time
import threading
from typing import Optional, Callable

log = logging.getLogger(__name__)

# Flipper Zero USB identifiers
FLIPPER_VID_PID = [(0x0483, 0x5740)]
FLIPPER_BAUD = 230400

# ANSI escape code stripper
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def detect_flipper_port() -> Optional[str]:
    """Auto-detect Flipper Zero serial port by VID:PID."""
    try:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            for vid, pid in FLIPPER_VID_PID:
                if port.vid == vid and port.pid == pid:
                    return port.device
    except Exception:
        pass
    return None


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from Flipper output."""
    return _ANSI_RE.sub('', text)


class FlipperManager:
    """Manages serial connection to Flipper Zero CLI."""

    def __init__(self) -> None:
        self._ser = None
        self._port: str = ""
        self._connected = False
        self._lock = threading.Lock()
        self._rx_thread: Optional[threading.Thread] = None
        self._rx_running = False
        self._on_line: Optional[Callable[[str], None]] = None
        self._subghz_active = False
        self.device_name: str = ""
        self.firmware: str = ""
        self.region: str = ""

    @property
    def connected(self) -> bool:
        return self._connected and self._ser is not None

    @property
    def port(self) -> str:
        return self._port

    def connect(self, port: str = "") -> bool:
        """Connect to Flipper Zero CLI."""
        if not port:
            port = detect_flipper_port()
        if not port:
            return False
        try:
            import serial
            self._ser = serial.Serial(port, FLIPPER_BAUD, timeout=1)
            self._port = port
            time.sleep(0.3)
            # Flush boot output
            while self._ser.in_waiting:
                self._ser.read(self._ser.in_waiting)
                time.sleep(0.1)
            # Send enter to get prompt
            self._ser.write(b"\r\n")
            time.sleep(0.3)
            while self._ser.in_waiting:
                self._ser.read(self._ser.in_waiting)
            self._connected = True
            log.info("Flipper connected on %s", port)
            # Get device info
            self._query_device_info()
            return True
        except Exception as exc:
            log.error("Flipper connect failed: %s", exc)
            self._connected = False
            return False

    def close(self) -> None:
        """Disconnect from Flipper."""
        self._rx_running = False
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=2)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._connected = False

    def reconnect(self) -> bool:
        """Try to reconnect if connection was lost (port changed)."""
        self.close()
        port = detect_flipper_port()
        if port:
            return self.connect(port)
        return False

    def ensure_connected(self) -> bool:
        """Ensure connection is alive, reconnect if needed."""
        if self.connected:
            try:
                self._ser.in_waiting  # test if port is alive
                return True
            except Exception:
                pass
        return self.reconnect()

    def send(self, cmd: str, timeout: float = 2.0) -> list[str]:
        """Send command and return response lines (blocking, short timeout)."""
        if not self.connected:
            return []
        with self._lock:
            try:
                # Flush
                while self._ser.in_waiting:
                    self._ser.read(self._ser.in_waiting)
                self._ser.write((cmd + "\r\n").encode())
                self._ser.flush()
                time.sleep(0.3)
                lines = []
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if self._ser.in_waiting:
                        raw = self._ser.readline().decode(errors="replace").strip()
                        clean = strip_ansi(raw)
                        if clean and clean != cmd and not clean.startswith(">"):
                            lines.append(clean)
                    else:
                        time.sleep(0.05)
                        if not self._ser.in_waiting:
                            break
                return lines
            except Exception as exc:
                log.error("Flipper send error: %s", exc)
                return []

    def send_async(self, cmd: str) -> None:
        """Send command without waiting for response."""
        if not self.connected:
            return
        with self._lock:
            try:
                self._ser.write((cmd + "\r\n").encode())
                self._ser.flush()
            except Exception:
                pass

    def start_rx(self, on_line: Callable[[str], None]) -> None:
        """Start background thread reading lines from Flipper."""
        self._on_line = on_line
        self._rx_running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    def stop_rx(self) -> None:
        """Stop background reading."""
        self._rx_running = False
        # Send enter to break any blocking read
        if self._ser:
            try:
                self._ser.write(b"\r\n")
            except Exception:
                pass

    def _rx_loop(self) -> None:
        """Background thread: read lines and call on_line callback."""
        while self._rx_running and self._ser:
            try:
                if self._ser.in_waiting:
                    raw = self._ser.readline().decode(errors="replace").strip()
                    clean = strip_ansi(raw)
                    if clean and self._on_line:
                        self._on_line(clean)
                else:
                    time.sleep(0.05)
            except Exception:
                if self._rx_running:
                    time.sleep(0.1)

    def _query_device_info(self) -> None:
        """Get device name, firmware, region."""
        lines = self.send("device_info")
        for line in lines:
            if "hardware_name" in line:
                self.device_name = line.split(":")[-1].strip()
            elif "firmware_version" in line:
                self.firmware = line.split(":")[-1].strip()
            elif "hardware_region_provisioned" in line:
                self.region = line.split(":")[-1].strip()

    # ------------------------------------------------------------------
    # SubGHz
    # ------------------------------------------------------------------

    def subghz_rx(self, freq: int = 433920000, device: int = 0) -> None:
        """Start SubGHz receive on given frequency."""
        self._subghz_active = True
        self.send_async(f"subghz rx {freq} {device}")

    def subghz_rx_raw(self, freq: int = 433920000) -> None:
        """Start SubGHz RAW receive."""
        self._subghz_active = True
        self.send_async(f"subghz rx_raw {freq}")

    def subghz_tx(self, key: str, freq: int = 433920000,
                  te: int = 400, repeat: int = 3, device: int = 0) -> None:
        """Transmit SubGHz key."""
        self.send_async(f"subghz tx {key} {freq} {te} {repeat} {device}")

    def subghz_tx_file(self, path: str, repeat: int = 1,
                       device: int = 0) -> None:
        """Transmit from .sub file on Flipper SD."""
        self.send_async(f"subghz tx_from_file {path} {repeat} {device}")

    def subghz_stop(self) -> None:
        """Stop SubGHz by sending enter."""
        self._subghz_active = False
        if self._ser:
            try:
                self._ser.write(b"\r\n")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def storage_list(self, path: str = "/ext/subghz") -> list[str]:
        """List files on Flipper SD card."""
        return self.send(f"storage list {path}", timeout=5.0)

    def storage_read(self, path: str) -> str:
        """Read file content from Flipper SD card."""
        if not self.connected:
            return ""
        with self._lock:
            try:
                while self._ser.in_waiting:
                    self._ser.read(self._ser.in_waiting)
                self._ser.write(f"storage read {path}\r\n".encode())
                self._ser.flush()
                time.sleep(0.5)
                lines = []
                deadline = time.time() + 5
                size_line = True
                while time.time() < deadline:
                    if self._ser.in_waiting:
                        raw = self._ser.readline().decode(errors="replace").strip()
                        clean = strip_ansi(raw)
                        if clean.startswith(">"):
                            if lines:
                                break
                            continue
                        if clean.startswith("storage read"):
                            continue
                        if size_line and clean.startswith("Size:"):
                            size_line = False
                            continue
                        lines.append(clean)
                    else:
                        time.sleep(0.05)
                        if not self._ser.in_waiting and lines:
                            break
                return "\n".join(lines)
            except Exception as exc:
                log.error("storage_read error: %s", exc)
                return ""

    # ------------------------------------------------------------------
    # NFC
    # ------------------------------------------------------------------

    def nfc_scan(self) -> None:
        """Start NFC scanner (enters NFC subshell)."""
        self.send_async("nfc")
        time.sleep(0.5)
        self.send_async("scanner")

    def nfc_emulate(self, filepath: str) -> list[str]:
        """Emulate NFC card from .nfc file on SD.

        Enters NFC subshell, runs emulate command, then exits.
        Emulation runs until stopped.
        """
        self.send_async("nfc")
        time.sleep(0.5)
        self.send_async(f"emulate -f {filepath}")

    def nfc_field(self, on: bool = True) -> None:
        """Toggle NFC field on/off."""
        self.send_async("nfc")
        time.sleep(0.3)
        self.send_async("field")

    def nfc_read_info(self) -> list[str]:
        """Read NFC tag info (enters NFC subshell, runs mfu info, exits)."""
        self.send("nfc")
        time.sleep(0.3)
        lines = self.send("mfu info")
        time.sleep(0.3)
        self.send("exit")
        return [l for l in lines if l and not l.startswith("[nfc]")]

    def nfc_dump(self, filepath: str, protocol: str = "14a") -> list[str]:
        """Dump NFC tag to file on Flipper SD."""
        self.send("nfc")
        time.sleep(0.3)
        lines = self.send(f"dump -p {protocol} -f {filepath}")
        time.sleep(2)
        self.send("exit")
        return lines

    def nfc_stop(self) -> None:
        """Stop NFC operation by sending enter + exit subshell."""
        if self._ser:
            try:
                self._ser.write(b"\r\n")
                time.sleep(0.3)
                self._ser.write(b"exit\r\n")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_battery(self) -> str:
        """Get battery info (if available)."""
        lines = self.send("power info")
        return "\n".join(lines) if lines else "N/A"

    def led(self, r: int, g: int, b: int) -> None:
        """Set Flipper LED color."""
        self.send_async(f"led {r} {g} {b}")

    def vibro(self, on: bool = True) -> None:
        """Toggle vibration."""
        self.send_async(f"vibro {1 if on else 0}")
