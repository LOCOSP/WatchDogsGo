"""SDR Manager — ADS-B aircraft tracking + 433 MHz sensor decoding.

Uses RTL-SDR (RTL2832U) on AIO v2 board via dump1090 and rtl_433.
Subprocess + thread + queue pattern (same as serial_manager).
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Callable, Optional

log = logging.getLogger(__name__)

# --- ADS-B Constants ---
# dump1090-fa/mutability: --net enables SBS on 30003 by default
# --quiet suppresses interactive output
DUMP1090_CMD = ["dump1090", "--net", "--quiet"]
# Fallback without --quiet (some builds don't support it)
DUMP1090_CMD_FALLBACK = ["dump1090", "--net"]

# --- rtl_433 Constants ---
RTL433_CMD = [
    "rtl_433",
    "-f", "433920000",            # 433.92 MHz
    "-F", "json",                 # JSON output to stdout
    "-M", "time:utc",            # UTC timestamps
    "-M", "level",               # include signal level
]

# Device index: dump1090 uses device 0, rtl_433 uses device 1
# (or run them sequentially, not simultaneously)
RTL433_CMD_DEV1 = RTL433_CMD + ["-d", "1"]

ADSB_TIMEOUT = 60  # remove aircraft not seen for 60s
SENSOR_TIMEOUT = 300  # remove sensors not seen for 5min


@dataclass
class Aircraft:
    """Tracked aircraft from ADS-B."""
    icao: str            # ICAO 24-bit hex address
    callsign: str = ""
    lat: float = 0.0
    lon: float = 0.0
    altitude: int = 0    # feet
    speed: int = 0       # knots
    heading: int = 0     # degrees
    squawk: str = ""
    last_seen: float = 0.0
    rssi: float = 0.0

    @property
    def has_position(self) -> bool:
        return self.lat != 0.0 or self.lon != 0.0


@dataclass
class Sensor433:
    """Decoded 433 MHz sensor from rtl_433."""
    model: str
    sid: str              # unique sensor ID (model + id combo)
    data: dict = field(default_factory=dict)
    lat: float = 0.0     # GPS position when first seen
    lon: float = 0.0
    last_seen: float = 0.0
    count: int = 0        # times seen


class SDRManager:
    """Manages dump1090 (ADS-B) and rtl_433 (ISM sensors) subprocesses."""

    def __init__(self):
        self.running = False
        self.mode: str = ""  # "adsb", "433", "adsb+433"

        # ADS-B state
        self.aircraft: dict[str, Aircraft] = {}  # icao -> Aircraft
        self.total_aircraft_seen: int = 0

        # 433 MHz state
        self.sensors: dict[str, Sensor433] = {}  # sid -> Sensor433
        self.total_sensors_seen: int = 0

        # Subprocess handles
        self._dump1090_proc: Optional[subprocess.Popen] = None
        self._rtl433_proc: Optional[subprocess.Popen] = None

        # Event queue for app.py
        self._events: Queue = Queue()
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

        # Callbacks
        self.on_new_aircraft: Optional[Callable] = None
        self.on_new_sensor: Optional[Callable] = None

        # Loot file handles
        self._adsb_log: Optional[object] = None
        self._sensor_log: Optional[object] = None

    @staticmethod
    def has_dump1090() -> bool:
        return shutil.which("dump1090") is not None

    @staticmethod
    def has_rtl433() -> bool:
        return shutil.which("rtl_433") is not None

    def start_adsb(self, loot_dir: str = "") -> bool:
        """Start ADS-B tracking via dump1090."""
        if not self.has_dump1090():
            self._events.put(("error", "dump1090 not installed"))
            return False
        if self._dump1090_proc:
            return True  # already running

        self._stop_event.clear()

        # Stop system dump1090 service if running (holds RTL-SDR device)
        try:
            subprocess.run(
                ["systemctl", "stop", "dump1090-mutability"],
                capture_output=True, timeout=5)
        except Exception:
            pass

        try:
            self._dump1090_proc = subprocess.Popen(
                DUMP1090_CMD,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            # Check if process died immediately (bad flags)
            time.sleep(0.5)
            if self._dump1090_proc.poll() is not None:
                # Retry without --quiet
                self._dump1090_proc = subprocess.Popen(
                    DUMP1090_CMD_FALLBACK,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
        except Exception as e:
            self._events.put(("error", f"dump1090 failed: {e}"))
            return False

        # Open loot file
        if loot_dir:
            try:
                self._adsb_log = open(
                    os.path.join(loot_dir, "adsb_aircraft.csv"), "a")
                if self._adsb_log.tell() == 0:
                    self._adsb_log.write(
                        "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,"
                        "heading,squawk\n")
            except Exception:
                pass

        # Start SBS parser thread (connects to port 30003)
        t = threading.Thread(target=self._sbs_reader, daemon=True)
        t.start()
        self._threads.append(t)

        self.running = True
        self.mode = "adsb"
        self._events.put(("status", "ADS-B started (dump1090)"))
        return True

    def start_433(self, loot_dir: str = "", gps_lat: float = 0.0,
                  gps_lon: float = 0.0) -> bool:
        """Start 433 MHz sensor decoding via rtl_433."""
        if not self.has_rtl433():
            self._events.put(("error", "rtl_433 not installed"))
            return False
        if self._rtl433_proc:
            return True

        self._stop_event.clear()
        self._gps_lat = gps_lat
        self._gps_lon = gps_lon

        # Use device 1 if dump1090 is already using device 0
        cmd = RTL433_CMD_DEV1 if self._dump1090_proc else RTL433_CMD
        try:
            self._rtl433_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            self._events.put(("error", f"rtl_433 failed: {e}"))
            return False

        # Open loot file
        if loot_dir:
            try:
                self._sensor_log = open(
                    os.path.join(loot_dir, "sdr_sensors.csv"), "a")
                if self._sensor_log.tell() == 0:
                    self._sensor_log.write(
                        "timestamp,model,id,data,lat,lon\n")
            except Exception:
                pass

        t = threading.Thread(target=self._rtl433_reader, daemon=True)
        t.start()
        self._threads.append(t)

        self.running = True
        if self.mode == "adsb":
            self.mode = "adsb+433"
        else:
            self.mode = "433"
        self._events.put(("status", "433 MHz scanner started (rtl_433)"))
        return True

    def stop(self):
        """Stop all SDR subprocesses."""
        self._stop_event.set()

        for proc, name in [
            (self._dump1090_proc, "dump1090"),
            (self._rtl433_proc, "rtl_433"),
        ]:
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        pass

        self._dump1090_proc = None
        self._rtl433_proc = None

        for f in (self._adsb_log, self._sensor_log):
            if f:
                try:
                    f.close()
                except Exception:
                    pass
        self._adsb_log = None
        self._sensor_log = None

        self.running = False
        self.mode = ""

    def poll_events(self) -> list:
        """Drain event queue. Returns list of (type, data) tuples."""
        events = []
        while not self._events.empty():
            try:
                events.append(self._events.get_nowait())
            except Exception:
                break
        return events

    def prune_stale(self):
        """Remove aircraft/sensors not seen recently."""
        now = time.time()
        stale_ac = [k for k, v in self.aircraft.items()
                    if now - v.last_seen > ADSB_TIMEOUT]
        for k in stale_ac:
            del self.aircraft[k]

        stale_s = [k for k, v in self.sensors.items()
                   if now - v.last_seen > SENSOR_TIMEOUT]
        for k in stale_s:
            del self.sensors[k]

    def update_gps(self, lat: float, lon: float):
        """Update GPS position for tagging new sensors."""
        self._gps_lat = lat
        self._gps_lon = lon

    # ------------------------------------------------------------------
    # SBS BaseStation format parser (ADS-B via dump1090 port 30003)
    # ------------------------------------------------------------------
    def _sbs_reader(self):
        """Connect to dump1090 SBS output and parse aircraft data."""
        import socket
        time.sleep(2)  # wait for dump1090 to start

        while not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(("127.0.0.1", 30003))
                sock.settimeout(2)
                buf = ""
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(4096).decode("ascii", errors="ignore")
                        if not chunk:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            self._parse_sbs_line(line.strip())
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                sock.close()
            except Exception:
                if self._stop_event.is_set():
                    return
                time.sleep(3)  # retry connection

    def _parse_sbs_line(self, line: str):
        """Parse SBS BaseStation format line.

        Format: MSG,type,ses,id,icao,id,date,time,date,time,
                callsign,alt,speed,heading,lat,lon,vrate,squawk,...
        """
        parts = line.split(",")
        if len(parts) < 11 or parts[0] != "MSG":
            return

        msg_type = parts[1].strip()
        icao = parts[4].strip().upper()
        if not icao:
            return

        now = time.time()
        is_new = icao not in self.aircraft
        ac = self.aircraft.setdefault(icao, Aircraft(icao=icao))
        ac.last_seen = now

        try:
            if msg_type == "1" and len(parts) > 10:
                cs = parts[10].strip()
                if cs:
                    ac.callsign = cs
            elif msg_type == "3" and len(parts) > 15:
                alt = parts[11].strip()
                spd = parts[12].strip()
                hdg = parts[13].strip()
                lat = parts[14].strip()
                lon = parts[15].strip()
                if alt:
                    ac.altitude = int(float(alt))
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
                if lat and lon:
                    ac.lat = float(lat)
                    ac.lon = float(lon)
            elif msg_type == "4" and len(parts) > 13:
                spd = parts[12].strip()
                hdg = parts[13].strip()
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
            elif msg_type == "6" and len(parts) > 17:
                sq = parts[17].strip()
                if sq:
                    ac.squawk = sq
        except (ValueError, IndexError):
            pass

        had_pos_before = not is_new and ac.has_position and msg_type != "3"

        if is_new:
            self.total_aircraft_seen += 1
            label = ac.callsign or icao
            self._events.put(("aircraft_new", ac))
            pos = f"pos:{ac.lat:.3f},{ac.lon:.3f}" if ac.has_position else "waiting for position..."
            self._events.put(("log",
                              f"[ADS-B] New: {label} "
                              f"alt:{ac.altitude}ft {pos}"))
            ac._logged_pos = ac.has_position
        elif msg_type == "3" and ac.has_position and not getattr(ac, '_logged_pos', False):
            # First position fix for this aircraft
            label = ac.callsign or icao
            self._events.put(("log",
                              f"[ADS-B] {label} located: "
                              f"{ac.lat:.3f},{ac.lon:.3f} "
                              f"alt:{ac.altitude}ft spd:{ac.speed}kt"))
            ac._logged_pos = True

        # Log to CSV
        if ac.has_position and self._adsb_log:
            try:
                self._adsb_log.write(
                    f"{int(now)},{icao},{ac.callsign},{ac.lat:.6f},"
                    f"{ac.lon:.6f},{ac.altitude},{ac.speed},"
                    f"{ac.heading},{ac.squawk}\n")
                self._adsb_log.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # rtl_433 JSON parser (433 MHz ISM sensors)
    # ------------------------------------------------------------------
    def _rtl433_reader(self):
        """Read rtl_433 JSON output line by line."""
        proc = self._rtl433_proc
        if not proc or not proc.stdout:
            return

        while not self._stop_event.is_set():
            try:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                self._parse_rtl433_line(
                    line.decode("utf-8", errors="replace").strip())
            except Exception:
                if self._stop_event.is_set():
                    return
                continue

    def _parse_rtl433_line(self, line: str):
        """Parse a JSON line from rtl_433."""
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        model = data.get("model", "Unknown")
        sid_num = data.get("id", data.get("channel", ""))
        sid = f"{model}_{sid_num}"
        now = time.time()

        is_new = sid not in self.sensors
        sensor = self.sensors.setdefault(sid, Sensor433(
            model=model, sid=sid,
            lat=getattr(self, "_gps_lat", 0.0),
            lon=getattr(self, "_gps_lon", 0.0),
        ))
        sensor.data = data
        sensor.last_seen = now
        sensor.count += 1

        if is_new:
            self.total_sensors_seen += 1
            self._events.put(("sensor_new", sensor))

        # Build display string
        display_parts = [f"[433] {model}"]
        for key in ("temperature_C", "humidity", "pressure_hPa",
                     "battery_ok", "wind_avg_km_h", "rain_mm"):
            if key in data:
                short = key.split("_")[0]
                display_parts.append(f"{short}:{data[key]}")
        rssi = data.get("rssi")
        if rssi is not None:
            display_parts.append(f"RSSI:{rssi}")
        self._events.put(("log", " ".join(display_parts)))

        # Log to CSV
        if self._sensor_log:
            try:
                safe_data = json.dumps(data, ensure_ascii=True)
                self._sensor_log.write(
                    f"{int(now)},{model},{sid_num},"
                    f"\"{safe_data}\","
                    f"{sensor.lat:.6f},{sensor.lon:.6f}\n")
                self._sensor_log.flush()
            except Exception:
                pass
