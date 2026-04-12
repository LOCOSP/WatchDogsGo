"""Serial communication with ESP32-C5 device."""

import os
import sys
import time
import logging
from typing import List, Optional, Callable, Tuple

import serial
import serial.tools.list_ports

from .config import BAUD_RATE, READ_TIMEOUT, SCAN_TIMEOUT, CRASH_KEYWORDS

log = logging.getLogger(__name__)

# Known USB VID:PID pairs for ESP32 USB-UART bridges
_ESP32_VID_PIDS: List[Tuple[int, int]] = [
    (0x10C4, 0xEA60),  # CP210x (Silicon Labs) — CP2102N on ESP32 devkits
    (0x1A86, 0x7523),  # CH340/CH341 (WCH) — common cheap ESP32 boards
    (0x1A86, 0x55D4),  # CH9102 (WCH) — newer variant
    (0x0403, 0x6001),  # FTDI FT232R
    (0x0403, 0x6015),  # FTDI FT231X
    (0x303A, 0x1001),  # Espressif native USB-JTAG (ESP32-S3/C3/C6/C5)
    (0x303A, 0x4001),  # Espressif native USB-CDC (ESP32-S2)
]


def detect_esp32_port() -> Optional[str]:
    """Auto-detect ESP32 serial port by scanning USB VID/PID.

    Checks /dev/ttyUSB0-3 and /dev/ttyACM0-3 for known ESP32 USB-UART
    bridge chips. Returns the first matching port, or None.
    """
    candidates = list_usb_serial_devices()
    esp = [c for c in candidates if c[2]]  # is_esp32 == True
    if esp:
        return esp[0][0]
    # Fallback: check if any ttyUSB/ttyACM devices exist
    for pattern in ["/dev/ttyUSB", "/dev/ttyACM"]:
        for i in range(4):
            dev = f"{pattern}{i}"
            if os.path.exists(dev):
                log.info("ESP32 fallback candidate: %s (no VID/PID)", dev)
                return dev
    return None


def list_usb_serial_devices() -> List[Tuple[str, str, bool]]:
    """List all USB serial devices with descriptions.

    Returns list of (device_path, description, is_esp32) tuples.
    Description includes chip name (e.g. 'CP2102N') and VID:PID.
    """
    try:
        ports = serial.tools.list_ports.comports()
    except Exception:
        return []

    result = []
    for port in ports:
        if port.vid is None or port.pid is None:
            continue
        is_esp32 = (port.vid, port.pid) in _ESP32_VID_PIDS
        desc = port.description or f"{port.vid:04X}:{port.pid:04X}"
        log.info(
            "USB serial: %s [%04X:%04X] %s %s",
            port.device, port.vid, port.pid, desc,
            "(ESP32)" if is_esp32 else "",
        )
        result.append((port.device, desc, is_esp32))

    result.sort(key=lambda x: x[0])
    return result


def list_wifi_interfaces() -> List[Tuple[str, str, str, str]]:
    """List all WiFi interfaces with driver and chipset info.

    Returns list of (iface_name, mode, driver, chipset) tuples.
    Mode is 'managed', 'monitor', 'AP', etc.
    """
    import subprocess
    result_list: List[Tuple[str, str, str, str]] = []
    try:
        proc = subprocess.run(
            ["iw", "dev"], capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return result_list

    current_iface = None
    current_type = "managed"
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            if current_iface:
                driver, chipset = _get_iface_info(current_iface)
                result_list.append((current_iface, current_type, driver, chipset))
            current_iface = stripped.split()[1]
            current_type = "managed"
        elif stripped.startswith("type "):
            current_type = stripped.split()[1]
    if current_iface:
        driver, chipset = _get_iface_info(current_iface)
        result_list.append((current_iface, current_type, driver, chipset))

    return result_list


def _get_iface_info(iface: str) -> Tuple[str, str]:
    """Get driver name and chipset for a network interface."""
    driver = ""
    chipset = ""
    driver_path = f"/sys/class/net/{iface}/device/driver"
    try:
        driver = os.path.basename(os.readlink(driver_path))
    except Exception:
        pass
    product_path = f"/sys/class/net/{iface}/device/../product"
    try:
        real = os.path.realpath(product_path)
        with open(real) as f:
            chipset = f.read().strip()
    except Exception:
        pass
    return driver, chipset


class SerialLineBuffer:
    """Accumulate raw bytes and yield complete newline-terminated lines.

    Used with urwid's ``watch_file`` callback where reads are non-blocking
    and may deliver partial lines.
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> List[str]:
        self._buf += raw
        lines: List[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                lines.append(decoded)
        return lines


class SerialManager:
    """Manage the serial connection to an ESP32-C5 device."""

    def __init__(self, device: str) -> None:
        self.device = device
        self.serial_conn: Optional[serial.Serial] = None
        self.baud_rate = BAUD_RATE
        self.line_buffer = SerialLineBuffer()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Open the serial port. Raises on failure."""
        if not os.path.exists(self.device):
            raise FileNotFoundError(f"Device {self.device} does not exist")

        if not os.access(self.device, os.R_OK | os.W_OK):
            raise PermissionError(
                f"No read/write access to '{self.device}'. "
                "Try: sudo usermod -a -G dialout $USER"
            )

        self.serial_conn = serial.Serial(
            port=self.device,
            baudrate=self.baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=READ_TIMEOUT,
            write_timeout=2,
        )
        self.serial_conn.reset_input_buffer()
        self.serial_conn.reset_output_buffer()
        log.info("Serial port %s opened at %d baud", self.device, self.baud_rate)

    def probe(self) -> bool:
        """Check if firmware is responsive.

        Checks for pending data first (boot banner).  If nothing pending,
        tries a quick write — on XIAO CDC, write blocks with
        SerialTimeoutException until firmware boots.  A successful write
        (no exception) means firmware is ready, even if it doesn't echo back.
        """
        if not self.serial_conn:
            return False
        try:
            # Check if anything is already waiting (boot output)
            if self.serial_conn.in_waiting > 0:
                return True
            # Try a non-blocking write probe — may timeout on XIAO CDC
            try:
                self.serial_conn.write(b"\r\n")
                self.serial_conn.flush()
                return True  # write succeeded = firmware is running
            except serial.SerialTimeoutException:
                return False  # write blocked = firmware not ready
            except Exception:
                return False
        except Exception:
            return False

    def close(self) -> None:
        if self.serial_conn:
            self.serial_conn.close()
            self.serial_conn = None

    @property
    def is_open(self) -> bool:
        return self.serial_conn is not None and self.serial_conn.is_open

    @property
    def fd(self) -> int:
        """Return the file descriptor for use with urwid watch_file."""
        if self.serial_conn is None:
            raise RuntimeError("Serial port not open")
        return self.serial_conn.fileno()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def send_command(self, command: str) -> None:
        if not self.serial_conn:
            log.error("Serial not connected — cannot send %r", command)
            return
        try:
            self.serial_conn.write((command + "\r\n").encode("utf-8"))
            self.serial_conn.flush()
            time.sleep(0.1)
            log.debug("TX: %s", command)
        except Exception as exc:
            log.error("Send error: %s", exc)

    def read_available(self) -> List[str]:
        """Non-blocking read: grab whatever bytes are waiting and return
        complete lines via the internal line buffer."""
        if not self.serial_conn:
            return []
        waiting = self.serial_conn.in_waiting
        if waiting <= 0:
            return []
        raw = self.serial_conn.read(waiting)
        return self.line_buffer.feed(raw)

    def read_response(self, timeout: float = SCAN_TIMEOUT, idle_timeout: float = 1.5) -> List[str]:
        """Blocking read with timeout — kept for legacy / direct-call use."""
        if not self.serial_conn:
            return []

        lines: List[str] = []
        start = time.time()
        last_data = time.time()

        while time.time() - start < timeout:
            if self.serial_conn.in_waiting:
                try:
                    line = self.serial_conn.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        lines.append(line)
                        last_data = time.time()
                except Exception:
                    continue
            else:
                if lines and (time.time() - last_data) >= idle_timeout:
                    break
                time.sleep(0.05)

        return lines

    # ------------------------------------------------------------------
    # Crash detection helper
    # ------------------------------------------------------------------

    @staticmethod
    def is_crash_line(line: str) -> bool:
        return any(kw in line for kw in CRASH_KEYWORDS)
