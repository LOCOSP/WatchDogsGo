"""LoRa SX1262 control via SPI — sniffer, scanner, balloon tracker.

Uses LoRaRF library for direct SPI communication with SX1262 on AIO v2 board.
Background threads with queue-based output (same pattern as FlashManager).

Hardware: SX1262 on /dev/spidev1.0
  IRQ=GPIO26, Busy=GPIO24, Reset=GPIO25
  DIO2 as RF switch, DIO3 TCXO voltage
"""

import hashlib
import hmac
import logging
import os
import re
import struct
import threading
import time
from dataclasses import dataclass
from queue import Queue
from typing import Optional

log = logging.getLogger(__name__)

# EU868 frequencies (Hz)
EU868_FREQUENCIES = [
    868_100_000,  # 868.1 MHz
    868_300_000,  # 868.3 MHz
    868_500_000,  # 868.5 MHz
    867_100_000,  # 867.1 MHz
    867_300_000,  # 867.3 MHz
    867_500_000,  # 867.5 MHz
    867_700_000,  # 867.7 MHz
    867_900_000,  # 867.9 MHz
]

# LoRa APRS frequencies (Hz) — 433 MHz ISM band
# Ref: SQ2CPA/LoRa_APRS_Balloon project
APRS_FREQUENCIES = [
    433_775_000,  # 433.775 MHz — primary APRS EU (SF12/CR5 "300bps")
    434_855_000,  # 434.855 MHz — secondary APRS (SF9/CR7 "1200bps")
    439_912_500,  # 439.9125 MHz — tertiary APRS
]

# APRS modulation profiles: (freq_hz, sf, cr, label)
APRS_PROFILES = [
    (433_775_000, 12, 5, "433.775 SF12/CR5 300bps"),
    (434_855_000,  9, 7, "434.855 SF9/CR7 1200bps"),
    (439_912_500, 12, 5, "439.9125 SF12/CR5 300bps"),
]

# Combined scanner frequencies (EU868 + APRS 433)
SCAN_FREQUENCIES = EU868_FREQUENCIES + APRS_FREQUENCIES

SPREADING_FACTORS = [7, 8, 9, 10, 11, 12]

# LoRa APRS packet prefix (3 bytes)
APRS_PREFIX = b"\x3c\xff\x01"

# Sniffer presets: (freq_hz, sf, cr, bw, label)
PRESET_MESHCORE = (869_618_000, 8, 5, 62_500, "MeshCore 869.618 SF8 BW62.5k CR5")
PRESET_MESHTASTIC = (869_525_000, 11, 8, 250_000, "Meshtastic 869.525 SF11 BW250k CR8")

# MeshCore protocol constants
MESHCORE_PUBLIC_PSK = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
MESHCORE_PUBLIC_HASH = 0x11
MESHCORE_SYNC_WORD = 0x1424  # private LoRa sync word
MESHCORE_PREAMBLE = 16


@dataclass
class MeshCoreChannel:
    """A MeshCore channel with name, PSK, and derived hash."""
    name: str        # "public", "#hiking", "private-1"
    psk: bytes       # 16-byte AES key
    ch_hash: int     # 1-byte channel hash (SHA256(psk)[0])
    is_hashtag: bool = False


def make_hashtag_channel(tag: str) -> MeshCoreChannel:
    """Derive channel from hashtag name. PSK = SHA256('#tag')[0:16]."""
    import hashlib
    full = f"#{tag}" if not tag.startswith("#") else tag
    digest = hashlib.sha256(full.encode("utf-8")).digest()
    return MeshCoreChannel(name=full, psk=digest[:16],
                           ch_hash=digest[0], is_hashtag=True)


def make_private_channel(name: str, psk_hex: str) -> MeshCoreChannel:
    """Create private channel from name + hex PSK."""
    import hashlib
    psk = bytes.fromhex(psk_hex)[:16]
    ch_hash = hashlib.sha256(psk).digest()[0]
    return MeshCoreChannel(name=name, psk=psk, ch_hash=ch_hash)


PUBLIC_CHANNEL = MeshCoreChannel("public", MESHCORE_PUBLIC_PSK,
                                  MESHCORE_PUBLIC_HASH, False)


def _user_home() -> str:
    """Return real user home directory (handles sudo)."""
    home = os.path.expanduser("~")
    if os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            home = os.path.expanduser(f"~{sudo_user}")
    return home


def _meshcore_config_path() -> str:
    """Path to MeshCore config JSON. Migrates legacy ~/.janos_meshcore.json
    to ~/.watchdogs_meshcore.json on first access."""
    home = _user_home()
    new_path = os.path.join(home, ".watchdogs_meshcore.json")
    legacy_path = os.path.join(home, ".janos_meshcore.json")
    if not os.path.exists(new_path) and os.path.exists(legacy_path):
        try:
            os.rename(legacy_path, new_path)
            log.info("Migrated %s -> %s", legacy_path, new_path)
        except OSError as exc:
            log.warning("Could not migrate legacy meshcore config: %s", exc)
            return legacy_path  # fall back to reading legacy in-place
    return new_path


def load_meshcore_config() -> dict:
    """Load ~/.watchdogs_meshcore.json. Returns dict with node_name, channels."""
    import json
    path = _meshcore_config_path()
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            channels = [PUBLIC_CHANNEL]
            for ch in data.get("channels", []):
                if ch.get("is_hashtag"):
                    channels.append(make_hashtag_channel(ch["name"]))
                elif ch.get("psk_hex") and ch["name"] != "public":
                    channels.append(make_private_channel(ch["name"], ch["psk_hex"]))
            data["_channels"] = channels
            return data
    except Exception as exc:
        log.warning("meshcore config load error: %s", exc)
    # Empty node_name signals "first run, please generate unique name from pubkey"
    return {"node_name": "", "_channels": [PUBLIC_CHANNEL]}


def save_meshcore_config(node_name: str, channels: list) -> None:
    """Save config to ~/.watchdogs_meshcore.json."""
    import json
    path = _meshcore_config_path()
    ch_list = []
    for ch in channels:
        if ch.name == "public":
            continue  # don't save public, always present
        d = {"name": ch.name, "is_hashtag": ch.is_hashtag}
        if not ch.is_hashtag:
            d["psk_hex"] = ch.psk.hex()
        ch_list.append(d)
    try:
        with open(path, "w") as f:
            json.dump({"node_name": node_name, "channels": ch_list}, f, indent=2)
    except Exception as exc:
        log.warning("meshcore config save error: %s", exc)

# MeshCore payload types (header bits 5:2)
MC_PAYLOAD_TYPES = {
    0x00: "Request",
    0x01: "Response",
    0x02: "TextMsg",
    0x03: "ACK",
    0x04: "Advert",
    0x05: "GrpTxt",
    0x06: "GrpData",
    0x07: "AnonReq",
    0x08: "PathRet",
    0x09: "Trace",
    0x0A: "Multi",
    0x0B: "Control",
    0x0F: "RawCustom",
}
MC_ROUTE_TYPES = {0: "TFlood", 1: "Flood", 2: "Direct", 3: "TDirect"}

# Hardware config (from /etc/meshtasticd/config.yaml)
SPI_BUS = 1
SPI_CS = 0
SPI_SPEED = 7_800_000
PIN_RESET = 25
PIN_BUSY = 24
PIN_IRQ = 26  # DIO1


class LoRaManager:
    """Background LoRa operations with queue-based output."""

    def __init__(self) -> None:
        self.queue: Queue = Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.running = False
        self.mode = ""  # "sniffer", "scanner", "tracker"
        self.packets_received = 0
        self._seen_packets: dict[bytes, float] = {}  # hash→timestamp
        self._on_node = None   # callback(node_id, type, name, lat, lon, rssi, snr)
        self._on_message = None  # callback(channel, message, rssi, hops)
        self._on_dm = None       # callback(from_id, message, rssi, hops)
        self._on_dm_ack = None   # callback(ack_hash) — DM delivery confirmed
        self._on_tx_confirm = None  # callback(dedup_key) — own msg retransmitted
        self._pending_dm_acks: dict[bytes, bool] = {}  # expected ack_hash → waiting
        self._tx_queue: Queue = Queue()  # MeshCore TX packets
        self._tx_dedup_keys: set[bytes] = set()  # keys of our own sent packets
        self._mc_keypair = None  # cached Ed25519 keypair
        self._radio_cfg = None   # (freq, sf, cr, bw, sync_word, preamble)
        self._mc_channels: list[MeshCoreChannel] = [PUBLIC_CHANNEL]
        self._mc_active_ch: int = 0  # index for TX
        self._known_pubkeys: dict[str, bytes] = {}  # node_id → pubkey

    def set_mc_channels(self, channels: list) -> None:
        """Set channel list (thread-safe snapshot)."""
        self._mc_channels = list(channels)
        if self._mc_active_ch >= len(self._mc_channels):
            self._mc_active_ch = 0

    def set_mc_active_channel(self, idx: int) -> None:
        """Set active TX channel."""
        if 0 <= idx < len(self._mc_channels):
            self._mc_active_ch = idx

    def _emit(self, line: str, attr: str = "default") -> None:
        self.queue.put((line, attr))

    def _init_radio(self):
        """Initialize SX1262 via SPI. Returns LoRa object or None."""
        try:
            from LoRaRF import SX126x
        except ImportError:
            self._emit(
                "LoRaRF not installed! Run: pip install LoRaRF", "error",
            )
            return None

        # Release leftover GPIO allocations from crashed/killed previous run
        try:
            import lgpio
            h = lgpio.gpiochip_open(4)  # RP1 on Pi 5 / CM5
            for pin in (PIN_RESET, PIN_BUSY, PIN_IRQ):
                try:
                    lgpio.gpio_free(h, pin)
                except Exception:
                    pass
            lgpio.gpiochip_close(h)
        except Exception:
            pass

        try:
            lora = SX126x()

            # begin() calls setSpi() + setPins() + reset internally
            if not lora.begin(
                bus=SPI_BUS,
                cs=SPI_CS,
                reset=PIN_RESET,
                busy=PIN_BUSY,
                irq=PIN_IRQ,
            ):
                self._emit("SX1262 not detected on SPI bus", "error")
                return None

            # SX1262-specific: DIO2 as RF switch, DIO3 as TCXO voltage
            lora.setDio2RfSwitch(True)
            lora.setDio3TcxoCtrl(lora.DIO3_OUTPUT_1_8, 10)

            lora.setRxGain(lora.RX_GAIN_BOOSTED)
            try:
                lora.setTxPower(22, lora.TX_POWER_SX1262)
            except Exception:
                try:
                    lora.setTxPower(22)
                except Exception:
                    pass
            self._emit("SX1262 radio initialized", "dim")
            return lora
        except Exception as exc:
            self._emit(f"Radio init failed: {exc}", "error")
            log.warning("SX1262 init failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Sniffer — listen on a single frequency/SF
    # ------------------------------------------------------------------

    def start_sniffer(
        self,
        freq: int = 868_100_000,
        sf: int = 7,
        cr: int = 5,
        bw: int = 125_000,
        label: str = "",
        sync_word: int = 0,
        preamble: int = 0,
    ) -> None:
        """Start LoRa sniffer on given frequency and spreading factor."""
        if self.running:
            return
        self._stop_event.clear()
        self.running = True
        self.mode = "sniffer"
        self.packets_received = 0
        self._seen_packets.clear()
        self._thread = threading.Thread(
            target=self._run_sniffer,
            args=(freq, sf, cr, bw, label, sync_word, preamble),
            daemon=True,
        )
        self._thread.start()

    def start_meshcore(self) -> None:
        """Start sniffer on MeshCore EU/UK Narrow (869.618 MHz)."""
        freq, sf, cr, bw, label = PRESET_MESHCORE
        self.start_sniffer(
            freq, sf, cr, bw, label,
            sync_word=MESHCORE_SYNC_WORD,
            preamble=MESHCORE_PREAMBLE,
        )
        self.mode = "meshcore"

    def start_meshtastic(self) -> None:
        """Start sniffer on Meshtastic Medium Fast (869.525 MHz)."""
        freq, sf, cr, bw, label = PRESET_MESHTASTIC
        self.start_sniffer(freq, sf, cr, bw, label)
        self.mode = "meshtastic"

    def _configure_radio(
        self, lora, freq: int, sf: int, cr: int, bw: int,
        sync_word: int = 0, preamble: int = 0,
    ) -> None:
        """Apply frequency, modulation, and optional sync/preamble."""
        # Cache params so _do_tx() can fully reconfigure after TX
        self._radio_cfg = (freq, sf, cr, bw, sync_word, preamble)
        lora.setFrequency(freq)
        lora.setLoRaModulation(sf, bw, cr, False)
        if sync_word:
            lora.setSyncWord(sync_word)
        if preamble:
            # setLoRaPacket(headerType, preambleLength, payloadLength, crcType)
            lora.setLoRaPacket(0x00, preamble, 255, True)

    def _run_sniffer(
        self, freq: int, sf: int, cr: int, bw: int, label: str,
        sync_word: int = 0, preamble: int = 0,
    ) -> None:
        lora = self._init_radio()
        if not lora:
            self.running = False
            return
        try:
            self._configure_radio(
                lora, freq, sf, cr, bw, sync_word, preamble,
            )
            tag = label or f"{freq / 1_000_000:.3f}MHz SF{sf}"
            self._emit(f"Sniffer started: {tag}", "success")

            # Pick packet handler based on mode
            handler = (
                self._handle_meshcore
                if self.mode == "meshcore"
                else self._handle_packet
            )

            # Use RX_CONTINUOUS for meshcore (narrow BW, can't
            # afford gaps between RX_SINGLE cycles)
            use_continuous = self.mode == "meshcore"
            if use_continuous:
                # Set IRQ mask explicitly (same as nuclear RX resume)
                lora._irqSetup(
                    lora.IRQ_RX_DONE | lora.IRQ_TIMEOUT
                    | lora.IRQ_HEADER_ERR | lora.IRQ_CRC_ERR,
                )
                lora.setRx(lora.RX_CONTINUOUS)
                # Remove GPIO callback — it races with our SPI poll
                # (callback clears IRQ before poll can read it)
                try:
                    import RPi.GPIO as _gpio
                    _gpio.remove_event_detect(lora._irq)
                except Exception:
                    pass

            errors = 0
            while not self._stop_event.is_set():
                try:
                    if use_continuous:
                        # Poll IRQ register directly — GPIO callback
                        # with bouncetime is unreliable, misses packets
                        time.sleep(0.05)
                        irq = lora.getIrqStatus()
                        if irq & lora.IRQ_RX_DONE:
                            lora.clearIrqStatus(0x03FF)
                            rxLen, rxIdx = lora.getRxBufferStatus()
                            lora._payloadTxRx = rxLen
                            lora._bufferIndex = rxIdx
                            if rxLen > 0:
                                handler(lora, tag)
                        elif irq & (lora.IRQ_CRC_ERR | lora.IRQ_HEADER_ERR):
                            lora.clearIrqStatus(0x03FF)
                        # Check TX queue between RX polls
                        if not self._tx_queue.empty():
                            self._do_tx(lora)
                        errors = 0
                        continue
                    else:
                        lora.request(lora.RX_SINGLE)
                        lora.wait(2)  # 2s timeout
                    if lora.available() > 0:
                        handler(lora, tag)
                    errors = 0
                except Exception as exc:
                    errors += 1
                    log.debug("Sniffer iteration error: %s", exc)
                    if errors >= 10:
                        self._emit(
                            "Too many radio errors, restarting...",
                            "warning",
                        )
                        lora.end()
                        time.sleep(1)
                        lora = self._init_radio()
                        if not lora:
                            self._emit("Radio reinit failed", "error")
                            return
                        self._configure_radio(
                            lora, freq, sf, cr, bw, sync_word, preamble,
                        )
                        if use_continuous:
                            lora.request(lora.RX_CONTINUOUS)
                            try:
                                import RPi.GPIO as _gpio
                                _gpio.remove_event_detect(lora._irq)
                            except Exception:
                                pass
                        errors = 0
        except Exception as exc:
            self._emit(f"Sniffer error: {exc}", "error")
            log.error("LoRa sniffer error: %s", exc)
        finally:
            self._cleanup_radio(lora)
            self._emit("Sniffer stopped.", "dim")

    # ------------------------------------------------------------------
    # Scanner — cycle through EU868 + APRS 433 frequencies × SFs
    # ------------------------------------------------------------------

    def start_scanner(self) -> None:
        """Start scanning EU868 + APRS 433 frequencies × spreading factors."""
        if self.running:
            return
        self._stop_event.clear()
        self.running = True
        self.mode = "scanner"
        self.packets_received = 0
        self._thread = threading.Thread(
            target=self._run_scanner, daemon=True,
        )
        self._thread.start()

    def _run_scanner(self) -> None:
        lora = self._init_radio()
        if not lora:
            self.running = False
            return
        try:
            total = len(SCAN_FREQUENCIES) * len(SPREADING_FACTORS)
            self._emit(
                f"Scanner: {len(EU868_FREQUENCIES)} EU868 + "
                f"{len(APRS_FREQUENCIES)} APRS freqs × "
                f"{len(SPREADING_FACTORS)} SFs = {total} combos",
                "success",
            )
            cycle = 0
            errors = 0
            while not self._stop_event.is_set():
                cycle += 1
                self._emit(f"── Scan cycle {cycle} ──", "dim")
                for freq in SCAN_FREQUENCIES:
                    if self._stop_event.is_set():
                        break
                    for sf in SPREADING_FACTORS:
                        if self._stop_event.is_set():
                            break
                        try:
                            freq_mhz = freq / 1_000_000
                            lora.setFrequency(freq)
                            lora.setLoRaModulation(sf, 125_000, 5, False)
                            lora.request(lora.RX_SINGLE)
                            lora.wait(0.5)  # 500ms per combo
                            if lora.available() > 0:
                                self._handle_packet(
                                    lora, f"{freq_mhz:.1f}MHz SF{sf}",
                                )
                            errors = 0
                        except Exception as exc:
                            errors += 1
                            log.debug("Scanner iteration error: %s", exc)
                            if errors >= 10:
                                self._emit(
                                    "Too many radio errors, restarting...",
                                    "warning",
                                )
                                lora.end()
                                time.sleep(1)
                                lora = self._init_radio()
                                if not lora:
                                    self._emit("Radio reinit failed", "error")
                                    return
                                errors = 0
        except Exception as exc:
            self._emit(f"Scanner error: {exc}", "error")
            log.error("LoRa scanner error: %s", exc)
        finally:
            self._cleanup_radio(lora)
            self._emit("Scanner stopped.", "dim")

    # ------------------------------------------------------------------
    # Balloon Tracker — APRS 433 + UKHAS 868 listener
    # ------------------------------------------------------------------

    def start_tracker(self) -> None:
        """Start balloon tracker cycling APRS 433 and UKHAS 868 frequencies."""
        if self.running:
            return
        self._stop_event.clear()
        self.running = True
        self.mode = "tracker"
        self.packets_received = 0
        self._thread = threading.Thread(
            target=self._run_tracker, daemon=True,
        )
        self._thread.start()

    def _run_tracker(self) -> None:
        lora = self._init_radio()
        if not lora:
            self.running = False
            return
        try:
            # Tracker profiles: APRS 433 MHz + UKHAS 868 MHz
            profiles = [
                # (freq_hz, sf, cr, bw, label)
                (433_775_000, 12, 5, 125_000, "433.775 SF12 APRS"),
                (434_855_000,  9, 7, 125_000, "434.855 SF9 APRS"),
                (868_100_000,  8, 5, 125_000, "868.1 SF8 UKHAS"),
            ]
            labels = ", ".join(p[4] for p in profiles)
            self._emit(f"Balloon tracker: {labels}", "success")
            self._emit(
                "Listening for LoRa APRS + UKHAS payloads...", "dim",
            )

            errors = 0
            while not self._stop_event.is_set():
                for freq, sf, cr, bw, label in profiles:
                    if self._stop_event.is_set():
                        break
                    try:
                        lora.setFrequency(freq)
                        lora.setLoRaModulation(sf, bw, cr, False)
                        lora.request(lora.RX_SINGLE)
                        lora.wait(3)  # 3s per profile
                        if lora.available() > 0:
                            data = self._read_packet(lora)
                            rssi = lora.packetRssi()
                            snr = lora.snr()
                            self.packets_received += 1
                            self._parse_balloon(data, rssi, snr, label)
                        errors = 0  # reset on success
                    except Exception as exc:
                        errors += 1
                        log.debug("Tracker iteration error: %s", exc)
                        if errors >= 10:
                            self._emit(
                                f"Too many radio errors, restarting...",
                                "warning",
                            )
                            lora.end()
                            time.sleep(1)
                            lora = self._init_radio()
                            if not lora:
                                self._emit("Radio reinit failed", "error")
                                return
                            errors = 0
        except Exception as exc:
            self._emit(f"Tracker error: {exc}", "error")
            log.error("LoRa tracker error: %s", exc)
        finally:
            self._cleanup_radio(lora)
            self._emit("Tracker stopped.", "dim")

    def _parse_balloon(
        self, data: bytearray, rssi: int, snr: float, tag: str = "",
    ) -> None:
        """Parse balloon payload — tries APRS, then UKHAS CSV, then raw."""
        # Try LoRa APRS format first (3-byte prefix \x3c\xff\x01)
        if len(data) >= 3 and data[:3] == APRS_PREFIX:
            self._parse_aprs(data[3:], rssi, snr, tag)
            return

        # Binary/encrypted data — show hex, skip text parsing
        if not self._is_printable(data):
            self._emit(
                f"[{self.packets_received}] {tag} {len(data)}B "
                f"RSSI:{rssi}dBm SNR:{snr:.1f}dB",
                "success",
            )
            self._emit(
                f"  [Encrypted] {data[:32].hex()}"
                + ("..." if len(data) > 32 else ""),
                "warning",
            )
            return

        try:
            text = data.decode("utf-8", errors="replace").strip()

            # Try APRS without prefix (some trackers omit it)
            if ">" in text and ("=" in text or "!" in text or "/" in text):
                self._parse_aprs(data, rssi, snr, tag)
                return

            # Try UKHAS CSV: $$CALL,ID,TIME,LAT,LON,ALT,...
            clean = text.lstrip("$").strip()
            parts = clean.split(",")
            if len(parts) >= 6:
                call, sid, tm = parts[0], parts[1], parts[2]
                lat, lon, alt = parts[3], parts[4], parts[5]
                self._emit(
                    f"UKHAS [{call}] #{sid} {tm} ({tag})",
                    "attack_active",
                )
                self._emit(
                    f"  Pos: {lat},{lon}  Alt:{alt}m  "
                    f"RSSI:{rssi}dBm SNR:{snr:.1f}dB",
                    "success",
                )
                if len(parts) > 6:
                    extra = ",".join(parts[6:])
                    self._emit(f"  Extra: {extra}", "dim")
            else:
                # Unknown text format — show as-is
                self._emit(
                    f"[{self.packets_received}] {tag} {len(data)}B "
                    f"RSSI:{rssi}dBm SNR:{snr:.1f}dB",
                    "success",
                )
                self._emit(f"  {text}", "dim")
        except Exception:
            self._emit(
                f"[{self.packets_received}] {data.hex()}", "dim",
            )

    def _parse_aprs(
        self, data: bytearray, rssi: int, snr: float, tag: str = "",
    ) -> None:
        """Parse LoRa APRS packet: CALL>DEST:=DDMM.MMN/DDDMM.MMEO .../A=AAAAAA

        Format from SQ2CPA/LoRa_APRS_Balloon:
          CALL-11>APLAIX:=DDMM.MMN/DDDMM.MMEO SSS/PPP/A=AAAAAA/comment
        Position: DDMM.MM = degrees + decimal minutes
        Altitude: /A=AAAAAA in feet (6 digits)
        Comment fields: P(pkt) S(sats) F(freq) O(power) N(flight) etc.
        """
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            self._emit(
                f"[{self.packets_received}] APRS raw: {data.hex()}", "dim",
            )
            return

        # Parse CALL>DEST:payload
        m = re.match(r"([^>]+)>([^:]+):(.*)", text, re.DOTALL)
        if not m:
            self._emit(
                f"[{self.packets_received}] APRS? {tag} "
                f"RSSI:{rssi}dBm SNR:{snr:.1f}dB",
                "success",
            )
            self._emit(f"  {text}", "dim")
            return

        callsign = m.group(1)
        dest = m.group(2)
        payload = m.group(3)

        self._emit(
            f"APRS [{callsign}] → {dest} ({tag})",
            "attack_active",
        )

        # Try to extract position: =DDMM.MMN/DDDMM.MMEO or !DDMM.MMN/DDDMM.MMEO
        pos = re.search(
            r"[=!](\d{4}\.\d{2}[NS])[/\\](\d{5}\.\d{2}[EW])",
            payload,
        )
        if pos:
            lat_str, lon_str = pos.group(1), pos.group(2)
            lat = self._aprs_to_decimal(lat_str)
            lon = self._aprs_to_decimal(lon_str)
            pos_text = f"  Pos: {lat:.5f},{lon:.5f}"
        else:
            pos_text = "  Pos: (compressed/unknown)"

        # Extract altitude /A=NNNNNN (feet)
        alt_m = re.search(r"/A=(-?\d+)", payload)
        if alt_m:
            alt_ft = int(alt_m.group(1))
            alt_meters = alt_ft * 0.3048
            pos_text += f"  Alt:{alt_meters:.0f}m ({alt_ft}ft)"

        pos_text += f"  RSSI:{rssi}dBm SNR:{snr:.1f}dB"
        self._emit(pos_text, "success")

        # Show comment/telemetry (after position data)
        comment = re.sub(
            r"[=!]\d{4}\.\d{2}[NS][/\\]\d{5}\.\d{2}[EW]\S*\s*",
            "", payload,
        ).strip()
        if comment:
            self._emit(f"  {comment}", "dim")

    @staticmethod
    def _aprs_to_decimal(coord: str) -> float:
        """Convert APRS DDMM.MMN or DDDMM.MME to decimal degrees."""
        hemisphere = coord[-1]
        numeric = coord[:-1]
        if hemisphere in ("N", "S"):
            degrees = float(numeric[:2])
            minutes = float(numeric[2:])
        else:  # E, W
            degrees = float(numeric[:3])
            minutes = float(numeric[3:])
        decimal = degrees + minutes / 60.0
        if hemisphere in ("S", "W"):
            decimal = -decimal
        return decimal

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _read_packet(self, lora) -> bytearray:
        """Read all available bytes from radio buffer."""
        data = bytearray()
        while lora.available() > 0:
            data.append(lora.read())
        return data

    @staticmethod
    def _is_printable(data: bytearray) -> bool:
        """Check if data is mostly printable ASCII (>60% threshold)."""
        if not data:
            return False
        printable = sum(1 for b in data if 32 <= b < 127)
        return printable / len(data) > 0.6

    def _handle_packet(self, lora, tag: str) -> None:
        """Read a packet, log it with hex + decoded text."""
        data = self._read_packet(lora)
        rssi = lora.packetRssi()
        snr = lora.snr()
        self.packets_received += 1

        self._emit(
            f"[{self.packets_received}] {tag} {len(data)}B "
            f"RSSI:{rssi}dBm SNR:{snr:.1f}dB",
            "success",
        )

        if not data:
            self._emit("  (empty packet)", "dim")
            return

        if self._is_printable(data):
            text = data.decode("utf-8", errors="replace")
            clean = "".join(
                c if c.isprintable() or c == " " else "." for c in text
            )
            self._emit(f"  TXT: {clean}", "dim")
        else:
            # Encrypted/binary — show hex only, no garbled ASCII
            self._emit(
                f"  [Encrypted] {data[:32].hex()}"
                + ("..." if len(data) > 32 else ""),
                "warning",
            )

    # ------------------------------------------------------------------
    # MeshCore packet decoder
    # ------------------------------------------------------------------

    def _handle_meshcore(self, lora, tag: str) -> None:
        """Read and decode a MeshCore packet."""
        data = self._read_packet(lora)
        rssi = lora.packetRssi()
        snr = lora.snr()
        self.packets_received += 1

        if len(data) < 2:
            self._emit(
                f"[{self.packets_received}] {tag} {len(data)}B "
                f"(too short)",
                "dim",
            )
            return

        # Parse header byte: 0bVVPPPPRR
        header = data[0]
        route_type = header & 0x03
        payload_type = (header >> 2) & 0x0F
        version = (header >> 6) & 0x03

        route_name = MC_ROUTE_TYPES.get(route_type, "?")
        type_name = MC_PAYLOAD_TYPES.get(payload_type, f"0x{payload_type:02X}")

        # Transport codes present for route 0 or 3
        offset = 1
        if route_type in (0, 3):
            offset += 4  # skip 4-byte transport codes

        if offset >= len(data):
            self._emit(
                f"[{self.packets_received}] {type_name} {route_name} "
                f"(truncated) RSSI:{rssi}",
                "warning",
            )
            return

        # Parse path length byte: 0bSSNNNNNN
        path_byte = data[offset]
        hash_size = ((path_byte >> 6) & 0x03) + 1
        hash_count = path_byte & 0x3F
        offset += 1
        path_len = hash_count * hash_size
        offset += path_len  # skip path data

        hops = hash_count
        payload = data[offset:] if offset < len(data) else bytearray()

        # Deduplicate retransmissions: hash header + payload (skip path)
        dedup_key = bytes(data[:1]) + bytes(payload)
        now = time.time()
        if dedup_key in self._seen_packets:
            if now - self._seen_packets[dedup_key] < 30:
                # Own TX retransmitted by another node = delivery confirm
                if dedup_key in self._tx_dedup_keys and self._on_tx_confirm:
                    try:
                        self._on_tx_confirm(dedup_key)
                    except Exception:
                        pass
                    self._tx_dedup_keys.discard(dedup_key)
                return  # retransmission, skip
        self._seen_packets[dedup_key] = now
        # Prune old entries every so often
        if len(self._seen_packets) > 50:
            self._seen_packets = {
                k: v for k, v in self._seen_packets.items()
                if now - v < 60
            }

        self._emit(
            f"[{self.packets_received}] MC {type_name} {route_name} "
            f"v{version} {hops}hop {len(data)}B "
            f"RSSI:{rssi}dBm SNR:{snr:.1f}dB",
            "success",
        )
        # Debug: show raw header bytes for comparison with TX
        self._emit(f"  RX hex[:{min(16, len(data))}]: {data[:16].hex()}", "dim")

        # Decode by type
        if payload_type == 0x04:
            self._decode_mc_advert(payload, rssi, snr)
        elif payload_type == 0x05:
            self._decode_mc_group_text(payload, rssi, hops)
        elif payload_type == 0x03:
            ack_hash = bytes(payload[:4])
            if ack_hash in self._pending_dm_acks:
                del self._pending_dm_acks[ack_hash]
                self._emit(f"  ACK matched! DM delivered", "success")
                if self._on_dm_ack:
                    try:
                        self._on_dm_ack(ack_hash)
                    except Exception:
                        pass
            else:
                self._emit(f"  ACK {payload.hex()}", "dim")
        elif payload_type == 0x02:
            self._decode_mc_dm(payload, rssi, hops)
        elif payload_type == 0x08:
            # PathReturn — encrypted, same format as DM:
            #   [dest_hash(1)][src_hash(1)][MAC(2)][ciphertext]
            # Decrypted: [path_len(1)][embedded_type(1)][embedded_data...]
            self._decode_mc_pathreturn(payload)
        elif payload_type in (0x00, 0x01):
            self._emit(f"  [Encrypted peer msg] {len(payload)}B", "dim")
        else:
            if payload:
                self._emit(f"  type=0x{payload_type:02x} {payload[:32].hex()}", "dim")

    def _decode_mc_advert(self, payload: bytearray, lora_rssi: float = 0, lora_snr: float = 0) -> None:
        """Decode MeshCore Advertisement (type 0x04) — plaintext."""
        if len(payload) < 100:  # 32 pubkey + 4 timestamp + 64 sig = 100
            self._emit(f"  Advert: {payload.hex()}", "dim")
            return

        import struct

        pubkey = payload[:32]
        timestamp = struct.unpack_from("<I", payload, 32)[0]
        # signature at 36:100
        flags_and_name = payload[100:]

        node_id = pubkey[:4].hex()
        # Skip our own advert (echoed back by repeaters)
        _, our_pub = self._get_ed25519_keypair()
        if bytes(pubkey) == our_pub:
            return
        self._known_pubkeys[node_id] = bytes(pubkey)
        self._emit(
            f"  Node: {node_id}... ts:{timestamp}",
            "attack_active",
        )

        if len(flags_and_name) >= 2:
            flags = flags_and_name[0]
            # Node types: 0=none, 1=chat/client, 2=repeater, 3=room, 4=sensor
            node_types = {0: "Client", 1: "Client", 2: "Repeater",
                          3: "Room", 4: "Sensor"}
            ntype = node_types.get(flags & 0x0F, "Unknown")

            # Check for GPS (bit 4) and name (bit 7)
            off = 1
            gps_str = ""
            if flags & 0x10 and len(flags_and_name) >= off + 8:
                lat_i = struct.unpack_from("<i", flags_and_name, off)[0]
                lon_i = struct.unpack_from("<i", flags_and_name, off + 4)[0]
                lat = lat_i / 1_000_000
                lon = lon_i / 1_000_000
                gps_str = f" GPS:{lat:.5f},{lon:.5f}"
                off += 8

            name = ""
            if off < len(flags_and_name):
                name_bytes = flags_and_name[off:]
                # Name is null-terminated; Repeater/Room nodes may
                # have extra radio-config bytes before it that happen
                # to be printable ASCII.  Find the null terminator and
                # search backwards for the real name.
                raw = name_bytes.split(b"\x00", 1)[0]
                decoded = raw.decode("utf-8", errors="ignore")
                # Name = last run of normal text chars reaching end
                m = re.search(r'[A-Za-z0-9][A-Za-z0-9 ._-]*$', decoded)
                name = m.group().strip() if m else ""

            self._emit(
                f"  [{ntype}] {name}{gps_str}",
                "success",
            )

            if self._on_node:
                lat_val = lat if (flags & 0x10) else 0.0
                lon_val = lon if (flags & 0x10) else 0.0
                try:
                    self._on_node(node_id, ntype, name, lat_val, lon_val,
                                  lora_rssi, lora_snr, bytes(pubkey))
                except Exception:
                    pass

    def _decode_mc_group_text(self, payload: bytearray, lora_rssi: float = 0, hops: int = 0) -> None:
        """Decode MeshCore Group Text (type 0x05) — try all configured channels."""
        if len(payload) < 4:
            self._emit(f"  GrpTxt: {payload.hex()}", "dim")
            return

        channel_hash = payload[0]
        mac = payload[1:3]
        ciphertext = payload[3:]

        if not ciphertext:
            return

        # Find matching channels by hash
        matched = [ch for ch in self._mc_channels if ch.ch_hash == channel_hash]
        if not matched:
            self._emit(
                f"  [ch:0x{channel_hash:02X}] {len(ciphertext)}B encrypted (unknown)",
                "dim",
            )
            return

        # Try decryption with each matching channel
        try:
            from cryptography.hazmat.primitives.ciphers import (
                Cipher, algorithms, modes,
            )
            for ch in matched:
                try:
                    cipher = Cipher(algorithms.AES(ch.psk), modes.ECB())
                    dec = cipher.decryptor()
                    pad_len = (16 - len(ciphertext) % 16) % 16
                    padded = bytes(ciphertext) + b"\x00" * pad_len
                    plaintext = dec.update(padded) + dec.finalize()

                    # Verify MAC
                    expected_mac = hmac.new(ch.psk, bytes(ciphertext),
                                           hashlib.sha256).digest()[:2]
                    if expected_mac != bytes(mac):
                        continue  # wrong channel, try next

                    ts = struct.unpack_from("<I", plaintext, 0)[0]
                    text_type = plaintext[4]
                    msg = plaintext[5:].split(b"\x00", 1)[0].decode(
                        "utf-8", errors="replace",
                    )
                    ch_label = ch.name
                    self._emit(f"  [{ch_label}] {msg}", "attack_active")
                    if self._on_message:
                        try:
                            self._on_message(ch_label, msg, lora_rssi, hops)
                        except Exception:
                            pass
                    return  # decoded successfully
                except Exception:
                    continue
            # All matched channels failed MAC check
            self._emit(
                f"  [ch:0x{channel_hash:02X}] MAC mismatch ({len(ciphertext)}B)",
                "dim",
            )
        except ImportError:
            self._emit(
                f"  [ch:0x{channel_hash:02X}] (need: pip install cryptography)",
                "warning",
            )
        except Exception as exc:
            self._emit(f"  [{ch_label}] decrypt err: {exc}", "warning")

    # ------------------------------------------------------------------
    # MeshCore TX — send group text and advertisements
    # ------------------------------------------------------------------

    def _preseed_dedup(self, packet: bytes) -> bytes:
        """Add TX packet to dedup cache so own retransmissions are filtered."""
        # dedup_key = header_byte + payload (skip path_byte at index 1)
        dedup_key = packet[0:1] + packet[2:]
        self._seen_packets[dedup_key] = time.time()
        self._tx_dedup_keys.add(dedup_key)
        return dedup_key

    def send_meshcore_message(self, text: str, node_name: str) -> bytes | None:
        """Queue a MeshCore public group text for transmission. Returns dedup_key."""
        if not self.running or self.mode != "meshcore":
            self._emit("  MeshCore not running — start sniffer first", "warning")
            return None
        packet = self._build_mc_group_text(text, node_name)
        dedup_key = self._preseed_dedup(packet)
        self._tx_queue.put(packet)
        return dedup_key

    def send_meshcore_dm(self, text: str, node_name: str,
                         dest_pubkey: bytes) -> bytes | None:
        """Queue an encrypted DM (payload type 0x02) to a specific node."""
        if not self.running or self.mode != "meshcore":
            self._emit("  MeshCore not running — start sniffer first", "warning")
            return None
        packet = self._build_mc_dm(text, node_name, dest_pubkey)
        if not packet:
            return None
        dedup_key = self._preseed_dedup(packet)
        self._tx_queue.put(packet)
        return dedup_key

    def send_meshcore_advert(self, node_name: str,
                             lat: float = 0.0, lon: float = 0.0) -> None:
        """Queue a MeshCore advertisement for transmission."""
        if not self.running or self.mode != "meshcore":
            self._emit("  MeshCore not running — start sniffer first", "warning")
            return
        packet = self._build_mc_advert(node_name, lat, lon)
        self._preseed_dedup(packet)
        self._tx_queue.put(packet)

    def _build_mc_group_text(self, text: str, node_name: str) -> bytes:
        """Build MeshCore Group Text packet on active channel."""
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )

        ch = self._mc_channels[self._mc_active_ch]

        # Plaintext: timestamp(4B LE) + flags(1B) + "name: msg\x00"
        timestamp = int(time.time())
        flags = 0x00  # text type=0, attempt=0
        msg = f"{node_name}: {text}\x00".encode("utf-8")
        plaintext = struct.pack("<I", timestamp) + bytes([flags]) + msg

        # Pad to 16-byte boundary
        pad_len = (16 - len(plaintext) % 16) % 16
        plaintext_padded = plaintext + b"\x00" * pad_len

        # AES-128-ECB encrypt with channel PSK
        cipher = Cipher(algorithms.AES(ch.psk), modes.ECB())
        enc = cipher.encryptor()
        ciphertext = enc.update(plaintext_padded) + enc.finalize()

        # MAC: HMAC-SHA256(PSK, ciphertext) truncated to 2 bytes
        mac = hmac.new(ch.psk, ciphertext, hashlib.sha256).digest()[:2]

        # Payload: channel_hash + mac + ciphertext
        payload = bytes([ch.ch_hash]) + mac + ciphertext

        # Packet: header + path_byte + payload (Flood routing)
        header = 0x15  # (0<<6)|(0x05<<2)|0x01 = Flood GrpTxt
        return bytes([header]) + b"\x00" + payload

    def _build_mc_advert(self, node_name: str,
                         lat: float = 0.0, lon: float = 0.0) -> bytes:
        """Build MeshCore Advertisement packet (type 0x04, Flood)."""
        privkey, pubkey_bytes = self._get_ed25519_keypair()

        timestamp = int(time.time())
        ts_bytes = struct.pack("<I", timestamp)

        # Appdata: flags + optional GPS + name
        # Flags: bits0-3=type(1=chat), bit4=GPS, bit5=feat1, bit6=feat2, bit7=name
        flags = 0x01  # type=1 (chat/client)
        flags |= 0x80  # has name (bit 7)
        has_gps = lat != 0.0 or lon != 0.0
        if has_gps:
            flags |= 0x10  # has GPS (bit 4)
        appdata = bytes([flags])
        if has_gps:
            appdata += struct.pack("<ii",
                                   int(lat * 1_000_000),
                                   int(lon * 1_000_000))
        appdata += node_name.encode("utf-8") + b"\x00"

        # Sign: pubkey(32) + timestamp(4) + appdata (MeshCore spec)
        sign_data = pubkey_bytes + ts_bytes + appdata
        signature = privkey.sign(sign_data)

        # Payload: pubkey(32) + timestamp(4) + signature(64) + appdata
        payload = pubkey_bytes + ts_bytes + signature + appdata

        # Packet: header + path_byte + payload
        header = 0x11  # (0<<6)|(0x04<<2)|0x01 = Flood Advert
        return bytes([header]) + b"\x00" + payload

    def _ecdh_shared_secret(self, dest_pubkey: bytes) -> bytes:
        """Derive 32-byte shared secret via X25519 (Ed25519→X25519 conversion).

        MeshCore: ed25519_key_exchange() clamps private key as X25519 scalar,
        converts peer Ed25519 pub to Montgomery form, does scalar mult.
        Returns raw 32-byte shared point (no hashing).
        """
        from cryptography.hazmat.primitives import serialization

        privkey, our_pub = self._get_ed25519_keypair()
        priv_raw = privkey.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        try:
            import nacl.bindings
            x_pub = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(
                dest_pubkey)
            x_priv = nacl.bindings.crypto_sign_ed25519_sk_to_curve25519(
                priv_raw + our_pub)  # nacl expects 64-byte sk
            shared = nacl.bindings.crypto_scalarmult(x_priv, x_pub)
        except ImportError:
            self._emit("  DM needs: pip install pynacl", "warning")
            raise
        return shared  # raw 32 bytes, NOT hashed

    @staticmethod
    def _compute_ack_hash(plaintext: bytes, text_len: int,
                          sender_pubkey: bytes) -> bytes:
        """Compute MeshCore ACK hash: SHA256(ts+flags+text, sender_pub)[:4].

        MeshCore uses a two-fragment SHA256:
          frag1 = plaintext[:5 + text_len]  (timestamp + flags + text)
          frag2 = sender_pubkey (32 bytes)
        """
        frag1 = plaintext[:5 + text_len]
        h = hashlib.sha256(frag1 + sender_pubkey).digest()
        return h[:4]

    def _build_ack(self, ack_hash: bytes) -> bytes:
        """Build unencrypted ACK packet (type 0x03, Flood)."""
        header = (0x03 << 2) | 0x01  # = 0x0D
        return bytes([header]) + b"\x00" + ack_hash

    def _send_ack(self, ack_hash: bytes) -> None:
        """Queue an ACK for transmission."""
        packet = self._build_ack(ack_hash)
        self._tx_queue.put(packet)
        self._emit(f"  TX ACK: {ack_hash.hex()}", "dim")

    def _build_mc_dm(self, text: str, node_name: str,
                     dest_pubkey: bytes) -> bytes | None:
        """Build MeshCore DM packet (type 0x02, Flood).

        MeshCore wire format:
          [dest_hash(1)] [src_hash(1)] [MAC(2)] [ciphertext(N*16)]
        Hash = pub_key[0] (first byte, no hashing).
        AES key = shared_secret[:16], HMAC key = shared_secret (32B).
        """
        try:
            shared = self._ecdh_shared_secret(dest_pubkey)
        except Exception as exc:
            self._emit(f"  DM key error: {exc}", "warning")
            return None

        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )

        _, our_pub = self._get_ed25519_keypair()
        # Hash = first byte of raw Ed25519 public key (NOT sha256!)
        dest_hash = dest_pubkey[0]
        src_hash = our_pub[0]

        # Plaintext: timestamp(4B LE) + flags(1B) + "name: text"
        timestamp = int(time.time())
        msg = f"{node_name}: {text}".encode("utf-8")
        plaintext = struct.pack("<I", timestamp) + b"\x00" + msg

        # Pre-compute expected ACK hash (sender=us, so our_pub)
        expected_ack = self._compute_ack_hash(plaintext, len(msg), our_pub)
        self._pending_dm_acks[expected_ack] = True

        # AES-128-ECB encrypt (key = first 16 bytes of shared secret)
        pad_len = (16 - len(plaintext) % 16) % 16
        plaintext_padded = plaintext + b"\x00" * pad_len
        aes_key = shared[:16]
        cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
        enc = cipher.encryptor()
        ciphertext = enc.update(plaintext_padded) + enc.finalize()

        # MAC: HMAC-SHA256(key=full 32B secret, data=ciphertext) → 2 bytes
        mac = hmac.new(shared, ciphertext, hashlib.sha256).digest()[:2]

        # Payload: dest_hash(1) + src_hash(1) + MAC(2) + ciphertext
        payload = bytes([dest_hash, src_hash]) + mac + ciphertext

        # Packet: header + path_byte + payload
        header = (0 << 6) | (0x02 << 2) | 0x01  # = 0x09
        return bytes([header]) + b"\x00" + payload

    def _decode_mc_pathreturn(self, payload: bytearray) -> None:
        """Decode PathReturn (0x08) — encrypted like DM, may contain ACK."""
        if len(payload) < 5:
            self._emit(f"  PathRet too short ({len(payload)}B)", "dim")
            return

        dest_hash = payload[0]
        src_hash = payload[1]
        mac = payload[2:4]
        ciphertext = payload[4:]

        _, our_pub = self._get_ed25519_keypair()
        if dest_hash != our_pub[0]:
            self._emit(f"  PathRet not for us", "dim")
            return

        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )
        for nid, pubkey in self._known_pubkeys.items():
            if pubkey[0] != src_hash:
                continue
            try:
                shared = self._ecdh_shared_secret(pubkey)
                expected_mac = hmac.new(
                    shared, bytes(ciphertext), hashlib.sha256).digest()[:2]
                if expected_mac != bytes(mac):
                    continue
                # Decrypt
                aes_key = shared[:16]
                cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
                dec = cipher.decryptor()
                plaintext = dec.update(bytes(ciphertext)) + dec.finalize()
                # Decrypted: [path_len(1)][embedded_type(1)][data...]
                path_len = plaintext[0]
                if len(plaintext) > 1 + path_len:
                    embedded_type = plaintext[1 + path_len]
                    embedded_data = plaintext[2 + path_len:]
                    if embedded_type == 0x03 and len(embedded_data) >= 4:
                        ack_hash = bytes(embedded_data[:4])
                        self._emit(
                            f"  PathRet ACK: {ack_hash.hex()}", "dim")
                        if ack_hash in self._pending_dm_acks:
                            del self._pending_dm_acks[ack_hash]
                            self._emit(
                                f"  ACK matched! DM delivered", "success")
                            if self._on_dm_ack:
                                try:
                                    self._on_dm_ack(ack_hash)
                                except Exception:
                                    pass
                        return
                self._emit(f"  PathRet decrypted (no ACK)", "dim")
                return
            except Exception:
                continue
        self._emit(f"  PathRet: can't decrypt", "dim")

    def _decode_mc_dm(self, payload: bytearray, rssi: float = 0,
                      hops: int = 0) -> None:
        """Try to decode a DM (type 0x02) addressed to us.

        Hash = pub_key[0]. AES key = secret[:16]. HMAC key = secret (32B).
        """
        if len(payload) < 5:
            return

        dest_hash = payload[0]
        src_hash = payload[1]
        mac = payload[2:4]
        ciphertext = payload[4:]

        _, our_pub = self._get_ed25519_keypair()
        our_hash = our_pub[0]  # first byte of pubkey, NOT sha256

        if dest_hash != our_hash:
            return  # not for us

        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )
        # Try all known nodes with matching src_hash (collisions possible)
        for nid, pubkey in self._known_pubkeys.items():
            if pubkey[0] != src_hash:
                continue
            try:
                shared = self._ecdh_shared_secret(pubkey)
                # Verify MAC: HMAC-SHA256(key=32B secret, data=ciphertext)
                expected_mac = hmac.new(
                    shared, bytes(ciphertext), hashlib.sha256).digest()[:2]
                if expected_mac != bytes(mac):
                    continue
                # Decrypt: AES-128-ECB, key=secret[:16]
                aes_key = shared[:16]
                cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
                dec = cipher.decryptor()
                plaintext = dec.update(bytes(ciphertext)) + dec.finalize()
                # Plaintext: timestamp(4) + flags(1) + text
                msg_raw = plaintext[5:].split(b"\x00", 1)[0]
                msg = msg_raw.decode("utf-8", errors="replace")
                text_len = len(msg_raw)
                self._emit(f"  [DM] {msg}", "attack_active")
                # Send ACK back (sender's pubkey for hash)
                ack_hash = self._compute_ack_hash(
                    plaintext, text_len, pubkey)
                self._send_ack(ack_hash)
                if self._on_dm:
                    try:
                        self._on_dm(nid, msg, rssi, hops)
                    except Exception:
                        pass
                return
            except Exception:
                continue
        self._emit(f"  [DM] encrypted (unknown sender)", "dim")

    def _get_ed25519_keypair(self):
        """Load or generate Ed25519 keypair from ~/.watchdogs_meshcore_key.
        Migrates legacy ~/.janos_meshcore_key on first access."""
        if self._mc_keypair:
            return self._mc_keypair

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives import serialization

        # Resolve real user home under sudo
        home = os.path.expanduser("~")
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            try:
                import pwd
                home = pwd.getpwnam(sudo_user).pw_dir
            except KeyError:
                pass
        key_path = os.path.join(home, ".watchdogs_meshcore_key")
        legacy_key_path = os.path.join(home, ".janos_meshcore_key")

        # One-time migration from legacy name
        if not os.path.isfile(key_path) and os.path.isfile(legacy_key_path):
            try:
                os.rename(legacy_key_path, key_path)
                log.info("Migrated meshcore key: %s -> %s",
                         legacy_key_path, key_path)
            except OSError as exc:
                log.warning("Could not migrate legacy meshcore key: %s", exc)
                key_path = legacy_key_path  # use legacy in-place

        if os.path.isfile(key_path):
            with open(key_path, "rb") as f:
                raw = f.read()
            privkey = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            privkey = Ed25519PrivateKey.generate()
            raw = privkey.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            with open(key_path, "wb") as f:
                f.write(raw)
            os.chmod(key_path, 0o600)

        pubkey_bytes = privkey.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self._mc_keypair = (privkey, pubkey_bytes)
        return self._mc_keypair

    def _do_tx(self, lora) -> None:
        """Transmit queued packets, then resume RX_CONTINUOUS.

        Nuclear approach: after TX, do FULL radio reconfiguration
        (frequency, modulation, sync word, packet params) because
        beginPacket()/endPacket() corrupt multiple internal states
        in LoRaRF that surgical fixes cannot fully restore.
        """
        while not self._tx_queue.empty():
            try:
                packet = self._tx_queue.get_nowait()
            except Exception:
                break
            try:
                lora.beginPacket()
                lora.write(list(packet), len(packet))
                lora.endPacket(5000)  # non-blocking! just initiates TX

                # Wait for actual TX_DONE (endPacket returns immediately)
                deadline = time.time() + 5.0
                tx_ok = False
                while time.time() < deadline:
                    irq = lora.getIrqStatus()
                    if irq & lora.IRQ_TX_DONE:
                        tx_ok = True
                        break
                    if irq & lora.IRQ_TIMEOUT:
                        break
                    time.sleep(0.01)

                if tx_ok:
                    self._emit(
                        f"  TX: {len(packet)}B sent", "success")
                else:
                    self._emit(
                        f"  TX: {len(packet)}B timeout!", "error")
            except Exception as exc:
                self._emit(f"  TX error: {exc}", "error")

        # ── Nuclear RX resume: full radio reconfiguration ──
        # beginPacket() corrupts: _bufferIndex, buffer base address,
        #   _fixLoRaBw500() may alter BW registers, _statusWait
        # endPacket() corrupts: payloadLength, IRQ mask, adds GPIO callback
        # Surgical fixes failed — reconfigure everything from scratch.
        lora.setStandby(lora.STANDBY_RC)
        time.sleep(0.01)  # let chip settle in standby
        lora.clearIrqStatus(0x03FF)

        # Reset internal LoRaRF state that beginPacket/endPacket corrupted
        lora._bufferIndex = 0
        lora._payloadTxRx = 0
        lora._statusWait = 0  # clear TX_DONE wait state
        lora.setBufferBaseAddress(0x00, 0x00)

        # Full reconfigure: freq, modulation, sync word, packet params
        if self._radio_cfg:
            freq, sf, cr, bw, sync_word, preamble = self._radio_cfg
            lora.setFrequency(freq)
            lora.setLoRaModulation(sf, bw, cr, False)
            if sync_word:
                lora.setSyncWord(sync_word)
            if preamble:
                lora.setLoRaPacket(0x00, preamble, 255, True)
            else:
                lora.setPacketParamsLoRa(
                    lora._preambleLength, lora._headerType,
                    255, lora._crcType, False,
                )
        else:
            # Fallback: at least restore packet params
            lora.setPacketParamsLoRa(
                lora._preambleLength, lora._headerType,
                255, lora._crcType, False,
            )

        # Re-arm RX with proper IRQ mask
        lora._irqSetup(
            lora.IRQ_RX_DONE | lora.IRQ_TIMEOUT
            | lora.IRQ_HEADER_ERR | lora.IRQ_CRC_ERR,
        )
        lora.setRx(lora.RX_CONTINUOUS)
        # Remove GPIO callback — we use SPI polling
        try:
            import RPi.GPIO as _gpio
            _gpio.remove_event_detect(lora._irq)
        except Exception:
            pass

    def _cleanup_radio(self, lora) -> None:
        """Release SPI without GPIO.cleanup() (preserves pin mode for reuse).

        LoRaRF's lora.end() calls gpio.cleanup() which clears BCM pin mode.
        Next begin() fails because setmode() only runs at module import time.
        We close SPI directly and skip gpio.cleanup().
        """
        try:
            if lora:
                lora.sleep(lora.SLEEP_COLD_START)
                from LoRaRF import SX126x as _sx
                _sx.spi.close()
        except Exception:
            pass
        self.running = False

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the background thread to stop and wait for cleanup."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
