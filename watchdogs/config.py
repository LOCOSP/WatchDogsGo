"""Constants and configuration for Watch Dogs Go."""

import os
from pathlib import Path


def _load_secrets() -> dict[str, str]:
    """Load KEY=VALUE pairs from secrets.conf at the project root."""
    secrets: dict[str, str] = {}
    conf = Path(__file__).resolve().parent.parent / "secrets.conf"
    if not conf.is_file():
        return secrets
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                secrets[key.strip()] = value.strip()
    except OSError:
        pass
    return secrets


def _secret(*keys: str, default: str = "") -> str:
    """Look up a secret by trying keys in order. Used for backward-compat
    when migrating env/config names (e.g. JANOS_* → WDG_*)."""
    for k in keys:
        v = _secrets.get(k)
        if v:
            return v
    return default


def _env(*keys: str, default: str = "") -> str:
    """Look up an env var by trying keys in order (new name first)."""
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


_secrets = _load_secrets()

BAUD_RATE = 460800
SCAN_TIMEOUT = 15
READ_TIMEOUT = 2
SNIFFER_UPDATE_INTERVAL = 1
PORTAL_UPDATE_INTERVAL = 2
EVIL_TWIN_UPDATE_INTERVAL = 2
HS_RESCAN_INTERVAL = 45  # seconds between handshake auto-rescan cycles (no selection)

# ESP32 serial commands
CMD_SCAN_NETWORKS = "scan_networks"
CMD_SHOW_SCAN_RESULTS = "show_scan_results"
CMD_START_SNIFFER = "start_sniffer"
CMD_START_SNIFFER_NOSCAN = "start_sniffer_noscan"
CMD_PACKET_MONITOR = "packet_monitor"
CMD_CHANNEL_VIEW = "channel_view"
CMD_SHOW_SNIFFER_RESULTS = "show_sniffer_results"
CMD_SHOW_SNIFFER_RESULTS_VENDOR = "show_sniffer_results_vendor"
CMD_CLEAR_SNIFFER_RESULTS = "clear_sniffer_results"
CMD_SHOW_PROBES = "show_probes"
CMD_SHOW_PROBES_VENDOR = "show_probes_vendor"
CMD_LIST_PROBES = "list_probes"
CMD_LIST_PROBES_VENDOR = "list_probes_vendor"
CMD_SNIFFER_DEBUG = "sniffer_debug"
CMD_START_SNIFFER_DOG = "start_sniffer_dog"
CMD_DEAUTH_DETECTOR = "deauth_detector"
CMD_SELECT_NETWORKS = "select_networks"
CMD_UNSELECT_NETWORKS = "unselect_networks"
CMD_SELECT_STATIONS = "select_stations"
CMD_UNSELECT_STATIONS = "unselect_stations"
CMD_START_EVIL_TWIN = "start_evil_twin"
CMD_START_DEAUTH = "start_deauth"
CMD_START_HANDSHAKE = "start_handshake"
CMD_START_HANDSHAKE_SERIAL = "start_handshake_serial"
CMD_SAVE_HANDSHAKE = "save_handshake"
CMD_SAE_OVERFLOW = "sae_overflow"
CMD_START_BLACKOUT = "start_blackout"
CMD_START_GPS_RAW = "start_gps_raw"
CMD_GPS_SET = "gps_set"
CMD_START_WARDRIVE = "start_wardrive"
CMD_START_PORTAL = "start_portal"
CMD_START_KARMA = "start_karma"
CMD_VENDOR = "vendor"
CMD_BOOT_BUTTON = "boot_button"
CMD_LED = "led"
CMD_CHANNEL_TIME = "channel_time"
CMD_DOWNLOAD = "download"
CMD_STOP = "stop"
CMD_WIFI_CONNECT = "wifi_connect"
CMD_LIST_HOSTS = "list_hosts"
CMD_LIST_HOSTS_VENDOR = "list_hosts_vendor"
CMD_ARP_BAN = "arp_ban"
CMD_REBOOT = "reboot"
CMD_PING = "ping"
CMD_LIST_SD = "list_sd"
CMD_SHOW_PASS = "show_pass"
CMD_LIST_DIR = "list_dir"
CMD_LIST_SSID = "list_ssid"
CMD_FILE_DELETE = "file_delete"
CMD_SELECT_HTML = "select_html"
CMD_SET_HTML = "set_html"
CMD_SET_HTML_BEGIN = "set_html_begin"
CMD_SET_HTML_END = "set_html_end"
CMD_SCAN_BT = "scan_bt"
CMD_SCAN_AIRTAG = "scan_airtag"

# Auto-update check (Watch Dogs Go)
APP_UPDATE_URL = "https://raw.githubusercontent.com/LOCOSP/esp32-watch-dogs/master/watchdogs/__init__.py"

# Firmware flash settings (ESP32-C5)
FIRMWARE_RELEASE_URL = "https://api.github.com/repos/LOCOSP/projectZero/releases/latest"
FLASH_CHIP = "esp32c5"
FLASH_MODE = "dio"
FLASH_FREQ = "80m"
FIRMWARE_DIR = "/tmp/wdg-firmware"

# Board-specific flash profiles
FLASH_BOARDS = {
    "wroom": {
        "label": "ESP32-C5 WROOM-1 (Dev Kit)",
        "baud": 460800,
        "before": "default-reset",
        "bin_name": "projectZerobyLOCOSP.bin",
        "offsets": {
            "bootloader.bin": "0x2000",
            "partition-table.bin": "0x8000",
            "projectZerobyLOCOSP.bin": "0x20000",
        },
    },
    "xiao": {
        "label": "XIAO ESP32-C5 (Seeed Studio, USB-JTAG)",
        "baud": 460800,
        "before": "usb-reset",
        "bin_name": "projectZerobyLOCOSP-xiao.bin",
        "offsets": {
            "bootloader.bin": "0x2000",
            "partition-table.bin": "0x8000",
            "projectZerobyLOCOSP-xiao.bin": "0x20000",
        },
    },
}

# Legacy aliases (backward compat)
FLASH_BAUD = FLASH_BOARDS["wroom"]["baud"]
FLASH_OFFSETS = FLASH_BOARDS["wroom"]["offsets"]

# GPS module — default: AIO UART on uConsole
# Override with env vars: WDG_GPS_DEVICE, WDG_GPS_BAUD (legacy: JANOS_GPS_*)
GPS_DEVICE = _env("WDG_GPS_DEVICE", "JANOS_GPS_DEVICE", default="/tmp/gps-pty")
GPS_BAUD_RATE = int(_env("WDG_GPS_BAUD", "JANOS_GPS_BAUD", default="9600"))
GPS_PRIVACY_NOISE_DEG = 0.01  # ±0.01° ≈ ±1.1km randomization in private mode

# WiGLE API (wardriving upload)
WIGLE_API_URL = "https://api.wigle.net/api/v2/file/upload"
WIGLE_API_NAME = _secret("WDG_WIGLE_NAME", "JANOS_WIGLE_NAME")
WIGLE_API_TOKEN = _secret("WDG_WIGLE_TOKEN", "JANOS_WIGLE_TOKEN")

# WPA-sec (handshake upload + password download)
WPASEC_URL = "https://wpa-sec.stanev.org/?submit"
WPASEC_DL_URL = "https://wpa-sec.stanev.org/?api&dl=1"
WPASEC_KEY = _secret("WDG_WPASEC_KEY", "JANOS_WPASEC_KEY")

# Firmware update check
FIRMWARE_RELEASE_URL = "https://api.github.com/repos/LOCOSP/projectZero/releases/latest"
FLASH_BOARDS = {
    "wroom": {
        "label": "ESP32-C5 WROOM-1 (Dev Kit)",
        "baud": 460800,
        "before": "default-reset",
        "bin_name": "projectZerobyLOCOSP.bin",
        "offsets": {
            "bootloader.bin": "0x2000",
            "partition-table.bin": "0x8000",
            "projectZerobyLOCOSP.bin": "0x20000",
        },
    },
    "xiao": {
        "label": "XIAO ESP32-C5 (Seeed Studio, USB-JTAG)",
        "baud": 460800,
        "before": "usb-reset",
        "bin_name": "projectZerobyLOCOSP-xiao.bin",
        "offsets": {
            "bootloader.bin": "0x2000",
            "partition-table.bin": "0x8000",
            "projectZerobyLOCOSP-xiao.bin": "0x20000",
        },
    },
}

# Sound notifications (terminal bell)
SOUND_ENABLED = _env("WDG_SOUND", "JANOS_SOUND", default="1") != "0"

# Firmware crash signatures (only true crash indicators, not normal boot messages)
CRASH_KEYWORDS = (
    "Guru Meditation",
    "Core  0 panic",
    "assert failed:",
)
