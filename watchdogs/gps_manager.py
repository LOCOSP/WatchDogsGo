"""GPS receiver — NMEA parser for UART/USB GPS modules."""

import glob
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

import serial

from .config import GPS_DEVICE, GPS_BAUD_RATE

log = logging.getLogger(__name__)


@dataclass
class GpsFix:
    """Snapshot of current GPS state."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    speed_knots: float = 0.0
    satellites: int = 0
    satellites_visible: int = 0
    fix_quality: int = 0       # 0=no fix, 1=GPS, 2=DGPS
    hdop: float = 99.9
    timestamp: str = ""        # UTC time from NMEA (hhmmss.ss)
    valid: bool = False


class _LineBuffer:
    """Accumulate raw bytes and yield complete NMEA sentences."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> List[str]:
        self._buf += raw
        lines: List[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded.startswith("$"):
                lines.append(decoded)
        # Prevent unbounded growth if no newlines arrive
        if len(self._buf) > 1024:
            self._buf = self._buf[-512:]
        return lines


class GpsManager:
    """Manage a UART GPS receiver via pyserial + urwid watch_file."""

    # True when user explicitly set the GPS device via env var
    _user_configured = bool(
        os.environ.get("WDG_GPS_DEVICE")
        or os.environ.get("JANOS_GPS_DEVICE")
    )

    def __init__(self, device: str = GPS_DEVICE,
                 baud: int = GPS_BAUD_RATE) -> None:
        self.device = device
        self._baud = baud
        self._conn: Optional[serial.Serial] = None
        self._buf = _LineBuffer()
        self.fix = GpsFix()
        self._available = False
        self._gsv_visible: dict = {}  # constellation prefix → satellite count

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Try to open GPS serial port. Returns True on success.
        If user set WDG_GPS_DEVICE (or legacy JANOS_GPS_DEVICE), use that directly.
        Otherwise probe default path for NMEA, then auto-detect USB GPS.
        Never raises — GPS is optional."""
        device = self.device

        if self._user_configured:
            # User explicitly set env var — trust it
            if self._try_open(device):
                return True
            log.info("GPS device %s (from env) not available — GPS disabled",
                     device)
            return False

        # No env var — probe default path, then auto-detect
        if os.path.exists(device) and self._probe_nmea(device, self._baud):
            if self._try_open(device):
                return True

        log.info("GPS not on %s, scanning for USB GPS...", device)
        detected = self._auto_detect(exclude={device})
        if detected:
            self.device = detected
            if self._try_open(detected):
                return True
        log.info("No GPS found — GPS disabled")
        return False

    @staticmethod
    def _probe_nmea(device: str, baud: int) -> bool:
        """Quick check if a serial port outputs NMEA sentences."""
        try:
            conn = serial.Serial(port=device, baudrate=baud, timeout=2)
            data = conn.read(512)
            conn.close()
            return b"$GP" in data or b"$GN" in data
        except Exception:
            return False

    def _try_open(self, device: str) -> bool:
        """Try to open a serial port as GPS. Returns True on success."""
        if not os.path.exists(device):
            return False
        if not os.access(device, os.R_OK):
            log.warning("No read access to %s", device)
            return False
        try:
            self._conn = serial.Serial(
                port=device,
                baudrate=self._baud,
                timeout=0,
            )
            self._conn.reset_input_buffer()
            self._available = True
            log.info("GPS opened: %s @ %d baud", device, self._baud)
            return True
        except Exception as exc:
            log.debug("GPS open failed on %s: %s", device, exc)
            return False

    @staticmethod
    def _auto_detect(exclude: Optional[set] = None) -> Optional[str]:
        """Scan common serial paths for a GPS device (NMEA probe)."""
        skip = exclude or set()
        candidates = sorted(
            glob.glob("/dev/ttyUSB*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyAMA*")
        )
        for path in candidates:
            if path in skip:
                continue
            if not os.access(path, os.R_OK):
                continue
            try:
                conn = serial.Serial(port=path, baudrate=9600, timeout=2)
                data = conn.read(512)
                conn.close()
                if b"$GP" in data or b"$GN" in data:
                    log.info("GPS auto-detected on %s", path)
                    return path
                log.debug("GPS probe %s — no NMEA data", path)
            except Exception:
                continue
        return None

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._available = False

    @property
    def fd(self) -> int:
        """File descriptor for urwid watch_file."""
        if self._conn is None:
            raise RuntimeError("GPS port not open")
        return self._conn.fileno()

    # ------------------------------------------------------------------
    # Reading & parsing
    # ------------------------------------------------------------------

    def read_available(self) -> List[str]:
        """Non-blocking read — return complete NMEA sentences."""
        if not self._conn:
            return []
        try:
            waiting = self._conn.in_waiting
            if waiting <= 0:
                return []
            raw = self._conn.read(waiting)
            return self._buf.feed(raw)
        except Exception as exc:
            log.debug("GPS read error: %s", exc)
            return []

    def process_sentences(self, sentences: List[str]) -> None:
        """Parse NMEA sentences and update self.fix."""
        for s in sentences:
            try:
                self._parse(s)
            except Exception as exc:
                log.debug("GPS parse error: %s — %s", s.strip(), exc)

    def _parse(self, sentence: str) -> None:
        # Strip checksum
        if "*" in sentence:
            sentence = sentence.split("*")[0]
        parts = sentence.split(",")
        if len(parts) < 3:
            return
        kind = parts[0]
        if kind in ("$GPGGA", "$GNGGA"):
            self._parse_gga(parts)
        elif kind in ("$GPRMC", "$GNRMC"):
            self._parse_rmc(parts)
        elif kind in ("$GPGSV", "$GLGSV", "$GNGSV", "$GBGSV", "$GAGSV"):
            self._parse_gsv(parts)

    def _parse_gga(self, p: List[str]) -> None:
        """$GPGGA: time, lat, N/S, lon, E/W, quality, sats, hdop, alt, ..."""
        if len(p) < 10:
            return
        self.fix.fix_quality = int(p[6]) if p[6] else 0
        self.fix.valid = self.fix.fix_quality > 0
        if p[1]:
            self.fix.timestamp = p[1]
        self.fix.satellites = int(p[7]) if p[7] else 0
        self.fix.hdop = float(p[8]) if p[8] else 99.9
        if p[2] and p[3]:
            self.fix.latitude = self._to_decimal(p[2], p[3])
        if p[4] and p[5]:
            self.fix.longitude = self._to_decimal(p[4], p[5])
        if p[9]:
            self.fix.altitude = float(p[9])

    def _parse_rmc(self, p: List[str]) -> None:
        """$GPRMC: time, status, lat, N/S, lon, E/W, speed, ..."""
        if len(p) < 8:
            return
        self.fix.valid = (p[2] == "A")
        if p[1]:
            self.fix.timestamp = p[1]
        if p[2] == "A":
            if p[3] and p[4]:
                self.fix.latitude = self._to_decimal(p[3], p[4])
            if p[5] and p[6]:
                self.fix.longitude = self._to_decimal(p[5], p[6])
            if p[7]:
                self.fix.speed_knots = float(p[7])

    def _parse_gsv(self, p: List[str]) -> None:
        """$xxGSV: total_msgs, msg_num, sats_in_view, ..."""
        if len(p) < 4:
            return
        prefix = p[0][:3]  # $GP, $GL, $GN, $GB, $GA
        total_visible = int(p[3]) if p[3] else 0
        self._gsv_visible[prefix] = total_visible
        self.fix.satellites_visible = sum(self._gsv_visible.values())

    @staticmethod
    def _to_decimal(value: str, direction: str) -> float:
        """Convert NMEA ddmm.mmmm to decimal degrees."""
        dot = value.index(".")
        degrees = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
        result = degrees + minutes / 60.0
        if direction in ("S", "W"):
            result = -result
        return result
