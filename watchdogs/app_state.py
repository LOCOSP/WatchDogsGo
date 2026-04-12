"""Centralized mutable application state."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Network:
    index: str = ""
    ssid: str = ""
    vendor: str = ""
    bssid: str = ""
    channel: str = ""
    auth: str = ""
    rssi: str = ""
    band: str = ""


@dataclass
class SnifferAP:
    ssid: str = ""
    channel: int = 0
    client_count: int = 0
    clients: List[str] = field(default_factory=list)


@dataclass
class ProbeEntry:
    ssid: str = ""
    mac: str = ""


@dataclass
class AppState:
    # Device
    device: str = ""
    device_description: str = ""  # USB device chip name (e.g. "CP2102N")
    connected: bool = False
    esp32_ready: bool = False  # True once firmware responds to probe
    wifi_interfaces: List = field(default_factory=list)  # [(iface, mode, driver, chipset)]

    # Scan
    networks: List[Network] = field(default_factory=list)
    scan_done: bool = False
    scanning: bool = False
    selected_networks: str = ""

    # Sniffer
    sniffer_running: bool = False
    sniffer_packets: int = 0
    sniffer_aps: List[SnifferAP] = field(default_factory=list)
    sniffer_probes: List[ProbeEntry] = field(default_factory=list)
    sniffer_buffer: List[str] = field(default_factory=list)

    # Attacks
    attack_running: bool = False
    blackout_running: bool = False
    sae_overflow_running: bool = False
    handshake_running: bool = False

    # Portal
    portal_running: bool = False
    portal_ssid: str = ""
    portal_html_files: List[str] = field(default_factory=list)
    selected_html_index: int = -1
    selected_html_name: str = ""
    submitted_forms: int = 0
    last_submitted_data: str = ""
    portal_client_count: int = 0
    portal_log: List[str] = field(default_factory=list)

    # Evil Twin
    evil_twin_running: bool = False
    evil_twin_ssid: str = ""
    evil_twin_captured_data: List[str] = field(default_factory=list)
    evil_twin_client_count: int = 0
    evil_twin_log: List[str] = field(default_factory=list)

    # Dragon Drain
    dragon_drain_running: bool = False
    dragon_drain_frames: int = 0

    # MITM
    mitm_running: bool = False
    mitm_packets: int = 0

    # BlueDucky
    bt_ducky_running: bool = False

    # RACE Attack
    race_running: bool = False

    # Watch Dogs Game
    game_running: bool = False

    # Bluetooth
    bt_wardriving_running: bool = False
    bt_wardriving_devices: int = 0
    bt_scan_running: bool = False
    bt_tracking_running: bool = False
    bt_tracking_mac: str = ""
    bt_airtag_running: bool = False
    bt_devices: int = 0
    bt_airtags: int = 0
    bt_smarttags: int = 0

    # GPS
    gps_available: bool = False
    gps_fix_valid: bool = False
    gps_latitude: float = 0.0
    gps_longitude: float = 0.0
    gps_altitude: float = 0.0
    gps_satellites: int = 0
    gps_satellites_visible: int = 0
    gps_fix_quality: int = 0
    gps_hdop: float = 99.9
    gps_external: bool = False  # external GPS connected via [g] in Add-ons

    # AIO v2 module
    aio_available: bool = False
    aio_gps: bool = False
    aio_lora: bool = False
    aio_sdr: bool = False
    aio_usb: bool = False

    # Wardriving
    wardriving_running: bool = False
    wardriving_networks: int = 0  # unique BSSIDs found

    # LoRa
    lora_running: bool = False
    lora_mode: str = ""  # "sniffer", "scanner", "tracker", "meshcore", "meshtastic"
    lora_packets: int = 0
    mc_nodes: int = 0
    mc_messages: int = 0

    # Add-ons
    flashing: bool = False
    aio_toggling: float = 0.0

    # Runtime
    start_time: float = 0.0
    firmware_crashed: bool = False
    crash_message: str = ""
    firmware_version: str = ""

    def any_attack_running(self) -> bool:
        return any([
            self.attack_running,
            self.blackout_running,
            self.sae_overflow_running,
            self.handshake_running,
            self.portal_running,
            self.evil_twin_running,
            self.bt_scan_running,
            self.bt_tracking_running,
            self.bt_airtag_running,
            self.dragon_drain_running,
            self.mitm_running,
            self.bt_ducky_running,
            self.race_running,
        ])

    def stop_all(self) -> None:
        """Reset all running flags. ESP32 'stop' halts everything."""
        self.attack_running = False
        self.blackout_running = False
        self.sae_overflow_running = False
        self.handshake_running = False
        self.sniffer_running = False
        self.wardriving_running = False
        self.portal_running = False
        self.evil_twin_running = False
        self.scanning = False
        self.bt_wardriving_running = False
        self.bt_scan_running = False
        self.bt_tracking_running = False
        self.bt_tracking_mac = ""
        self.bt_airtag_running = False
        self.dragon_drain_running = False
        self.mitm_running = False
        self.bt_ducky_running = False
        self.race_running = False

    def reset_sniffer(self) -> None:
        self.sniffer_running = False
        self.sniffer_packets = 0
        self.sniffer_aps.clear()
        self.sniffer_probes.clear()
        self.sniffer_buffer.clear()

    def reset_portal(self) -> None:
        self.portal_running = False
        self.portal_ssid = ""
        self.submitted_forms = 0
        self.last_submitted_data = ""
        self.portal_client_count = 0
        self.portal_log.clear()

    def reset_evil_twin(self) -> None:
        self.evil_twin_running = False
        self.evil_twin_ssid = ""
        self.evil_twin_captured_data.clear()
        self.evil_twin_client_count = 0
        self.evil_twin_log.clear()
