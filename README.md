# Watch Dogs Go

Open-world hacking RPG with real cybersecurity tooling. A pyxel game frontend for the ESP32-C5 security device, inspired by Watch Dogs aesthetics.

![Watch Dogs Go — main screen](docs/screenshots/01_main_screen.png)

> *Lv.13 GHOST · 203k XP · 21 badges · synced with the wdgwars.pl community server*

![Loot Database](docs/screenshots/02_loot.png)

> *Loot database — 588 sessions, 6686 WiFi networks, 474 handshakes, 500 MeshCore nodes, BLACK HAT classification.*

![SYSTEM menu](docs/screenshots/03_system_menu.png)

> *SYSTEM menu — hardware toggles (GPS, LoRa, SDR, USB), whitelist, WPA-sec upload/download, ESP32 flasher. The hacker sprite changes per menu tab.*

![ADDONS menu](docs/screenshots/04_addons_menu.png)

> *ADDONS menu — MeshCore Messenger (LoRa mesh), Flipper Zero, ADS-B Radar, 433 MHz Scanner, PipBoy Watch. Each tab has its own sprite — here the radio recon operator with antenna and mesh nodes.*

![MeshCore Messenger](docs/screenshots/05_meshcore.png)

> *MeshCore Messenger — encrypted off-grid chat over LoRa 869 MHz. Node identity `WDG_locosp` was synced from the wdgwars.pl portal username automatically. 107 contacts visible from a single Polish MeshCore mesh, RSSI from -20dB to -113dB.*

![SCAN menu](docs/screenshots/06_scan_menu.png)

> *SCAN menu — entry point for WiFi and BLE scanning, MAC capture, sniffer modes. The cyber recon operator sprite watches over an arsenal of devices.*

Landing page: [locosp.org](https://locosp.org) — choose your path.

### Quick Install (uConsole / Raspberry Pi OS / Debian)

```bash
curl -sL https://locosp.github.io/esp32-watch-dogs/install | sudo bash
```

The installer clones the repo to `~/python/esp32-watch-dogs/`, creates a Python virtual environment, installs all dependencies, and adds a desktop launcher.

> **Optimized for [ClockworkPi uConsole](https://www.clockworkpi.com/uconsole)** — runs on 640x360 display at 30 FPS. Designed for field use with integrated hardware modules.

### First Run

After installation:

```bash
cd ~/python/esp32-watch-dogs
sudo ./run.sh
```

Or double-click **Watch Dogs Go** on your desktop.

**On first launch the game will:**

1. Load player profile (XP, level, badges) from `loot/loot_db.json`
2. Auto-detect ESP32 on `/dev/ttyUSB0` or `/dev/ttyACM0` (no serial = "ESP32 not found", that's OK to test the UI)
3. Try to open GPS on `/dev/ttyAMA0` (uConsole AIO) or USB GPS — optional
4. Discover and load plugins from `plugins/` (currently: Wars Sync, JanOS Loot Import)
5. Show the cyberdeck UI

**You don't need any API keys to start.** Wars Sync (community server upload) is locked until level 6 (WARDRIVER, 6000 XP) — until then you can play offline.

If you want to use the community server (wdgwars.pl), edit `secrets.conf`:

```bash
cp secrets.conf.example secrets.conf
nano secrets.conf
```

### What gets installed

The installer pulls these system packages (Debian/Ubuntu):

- `python3-venv`, `libsdl2-dev`, `libsdl2-image-dev` — for pyxel game engine
- `tcpdump`, `aircrack-ng` — for MITM and Dragon Drain attacks
- `dump1090-mutability`, `rtl-433` — for RTL-SDR (ADS-B and 433 MHz)
- `bluez`, `bluez-tools`, `pulseaudio-utils` — for BLE attacks
- `python3-rpi-lgpio` — GPIO library for Raspberry Pi 5 / CM5

And these Python packages (in `.venv`):

- `pyxel` — retro game engine
- `pyserial` — ESP32 serial communication
- `Pillow` — sprite generation
- `scapy`, `netifaces` — packet manipulation (MITM, Dragon Drain)
- `bleak`, `dbus-python` — BLE attacks (RACE, BLE HID)
- `LoRaRF`, `cryptography`, `PyNaCl` — LoRa MeshCore radio

### Known platform notes

- **Linux only** — macOS and Windows are not supported.
- **Raspberry Pi OS Bookworm/Trixie**: requires `python3-rpi-lgpio` (auto-installed). Older `RPi.GPIO` from pip doesn't work on RPi5/CM5.
- **Generic Linux**: Pyxel requires SDL2 system libraries. The installer handles Debian/Ubuntu automatically; for Fedora/Arch/Alpine you'll need to install SDL2 manually.
- **dialout group**: Your user must be in `dialout` group for ESP32 serial access without sudo. If you see "permission denied" on serial:
  ```bash
  sudo usermod -a -G dialout $USER
  ```
  Then log out and back in.

## Hardware Requirements

### Required

| Component | Description |
|-----------|-------------|
| **ClockworkPi uConsole** | Primary platform (or any Linux with pyxel-compatible display) |
| **ESP32-C5** | Running [projectZero](https://github.com/LOCOSP/projectZero) firmware — WiFi/BLE scanning, deauth, handshake capture, Evil Twin, BLE HID |
| **GPS module** | AIO v2 GPS on `/dev/ttyAMA0` (or USB GPS) — real-time positioning, wardriving logs, map tracking |

### Optional (for full functionality)

| Component | Description |
|-----------|-------------|
| **External WiFi adapter** (monitor mode) | Required for **Dragon Drain** (WPA3 SAE flood). Recommended: **Alfa Network AWUS036ACH** or **AWUS036ACM** |
| **LoRa SX1262 module** | AIO v2 LoRa — packet sniffing, MeshCore mesh scanning, Meshtastic, APRS balloon tracking |
| **Flipper Zero** | USB-connected — SubGHz RX/TX, NFC read/emulate, signal replay from SD card |

> Attacks marked with a red **!** triangle in the menu require an external WiFi adapter with monitor mode support.

### AIO v2 module (uConsole only)

If you have a [HackerGadgets AIO v2](https://github.com/hackergadgets/aiov2_ctl)
module plugged into your uConsole, the game can toggle GPS / LoRa / SDR / USB
power rails on demand from the SYSTEM menu. This is what makes the "GPS [g]",
"LoRa [l]", "SDR [d]" and "USB [b]" entries actually do something.

To enable AIO v2 control you need **two** things on the system:

1. **`pinctrl`** — already present on Raspberry Pi OS (provided by the `raspi-utils`
   package). Verify with `command -v pinctrl`. The game uses `pinctrl set/get`
   directly to flip the GPIO pins — this is faster and more reliable than going
   through the full `aiov2_ctl` subprocess chain at runtime.

2. **`aiov2_ctl`** — official AIO v2 control tool from HackerGadgets. Used by
   the game for hardware presence detection and first-time rail setup, and
   independently provides a CLI + system tray GUI. Install it with the
   official method:
   ```bash
   sudo apt install -y python3 python3-pyqt6 git
   git clone https://github.com/hackergadgets/aiov2_ctl.git
   cd aiov2_ctl
   sudo python3 ./aiov2_ctl.py --install
   ```
   This installs `aiov2_ctl` to `/usr/local/bin` and enables the
   `aiov2-rails-boot.service` systemd unit so the rails come up at boot.

   The game's `setup.sh` will run these steps automatically on uConsole-class
   hardware (when `pinctrl` is present), and the in-game SYSTEM menu also
   offers an "Install aiov2_ctl" action if it isn't found.

Without these the game still runs fine, the AIO toggles just become no-ops
and their state always shows `OFF` in the SYSTEM tab.

## What It Is

A pyxel-based game where you walk around a real-world map while your ESP32 scans for WiFi networks and BLE devices. Captured handshakes, credentials, GPS coordinates, and network data are saved to disk. The game is a visual overlay on real security tooling.

- Per-tab character sprites (SCAN, SNIFF, ATTACK, ADDONS, SYSTEM)
- Cyberdeck menu system with 5 categories
- Real-time radar, map markers, particle effects
- Terminal with live ESP32 output and attack logs
- World map with coastlines, tile rendering, zoom levels
- Persistent XP system with 20 rank levels (NOOB → FINAL_BOSS)
- Smart XP — full points for new devices, 1 XP for duplicates
- Hacker hat classification (WHITE/BLUE/GREY/RED/BLACK) based on activity profile
- Achievement badges: FLIPPER, WARDRIVER, MESHCORE, HS HUNTER, WPA-SEC, EVIL TWIN
- Flipper Zero integration — SubGHz scanner/replay, NFC read/emulate
- MeshCore toast notifications — always-on-top across all screens
- Auto-reconnect ESP32 after USB replug
- Firmware version check + OTA update notification + ESP32 flasher

## Install

```bash
cd ~/python/esp32-watch-dogs
bash setup.sh
```

Requires Python 3.10+ and SDL2 libraries (auto-installed by setup.sh).

## Run

```bash
# Auto-detect ESP32 and GPS:
./run.sh

# Specify serial port:
./run.sh /dev/ttyUSB0

# Run as module (sudo needed for scapy/airmon/tcpdump):
sudo .venv/bin/python3 -m watchdogs
```

Or click the **Watch Dogs Go** desktop icon on the uConsole.

## Controls

### General

| Key | Action |
|-----|--------|
| `TAB` | Open / close cyberdeck menu |
| `SPACE` (hold) | Hack nearby device |
| `S` | Quick stop — all ESP32 + Python attacks |
| `ESC` | Quit game (sends stop to ESP32) |
| `` ` `` (backtick) | Toggle loot screen |

### Map Navigation

| Key | Action |
|-----|--------|
| `Arrow keys` | Pan map manually (GPS overrides when fix available) |
| `=` or `]` | Zoom in |
| `-` or `[` | Zoom out |
| `0` | Reset zoom to world view |

### Terminal

| Key | Action |
|-----|--------|
| `PgUp` / `PgDn` | Scroll terminal history |
| `Fn+U` / `Fn+K` | PgUp / PgDn on uConsole keyboard |

### Cyberdeck Menu (TAB)

| Key | Action |
|-----|--------|
| `LEFT` / `RIGHT` | Switch category tab |
| `UP` / `DOWN` | Navigate items |
| `ENTER` | Select / execute item |
| `ESC` | Close menu |

### Input Dialogs

| Key | Action |
|-----|--------|
| `A-Z`, `0-9` | Type characters |
| `BACKSPACE` | Delete last character |
| `ENTER` | Confirm input / next field |
| `ESC` | Cancel |

### MITM Sub-Screen

| Key | Action |
|-----|--------|
| `S` | Start attack (from idle) |
| `UP` / `DOWN` | Navigate lists |
| `ENTER` | Select |
| `Y` / `N` | Confirm / cancel |
| `X` | Stop running attack |
| `PgUp` / `PgDn` | Scroll live log |
| `ESC` | Back / exit |

## Cyberdeck Menu

### SCAN

| Item | Command | Description |
|------|---------|-------------|
| WiFi Scan | `scan_networks` | Scan nearby WiFi access points |
| BLE Scan | `scan_bt` | Scan Bluetooth Low Energy devices |
| BT Tracker | `bt_track` | Track specific BT device by MAC |
| AirTag Scan | `bt_airtag_scan` | Detect Apple AirTags nearby |

### SNIFF

| Item | Command | Description |
|------|---------|-------------|
| WiFi Wardrive | `scan_networks` | Continuous WiFi scan + GPS logging (WiGLE CSV) |
| BT Wardrive | `scan_bt` | Continuous BLE scan + GPS logging |
| Pkt Sniffer | `start_sniffer` | Raw 802.11 + BLE packet capture |
| HS Serial | `start_handshake_serial` | WPA handshake capture via serial |

### ATTACK

| Item | Command | Description |
|------|---------|-------------|
| Deauth | `start_deauth` | Targeted deauth on BSSID + channel |
| Blackout | `start_blackout` | All-channel deauth broadcast |
| HS Capture | `start_handshake_serial` | WPA handshake capture |
| Evil Twin | `start_portal` | Fake AP with captive portal (SSID input) |
| SAE Flood | `sae_overflow` | WPA3 SAE Commit overflow |
| Dragon Drain | Python-native | WPA3 SAE DoS via scapy **!** |
| MITM | Python-native | ARP spoofing + live traffic capture |
| BlueDucky | Python-native | BLE HID keystroke injection (CVE-2023-45866) |
| RACE Attack | Python-native | Airoha BT headphone exploit (CVE-2025-20700) |

> **!** = requires external WiFi adapter with monitor mode

### ADDONS

| Item | Command | Description |
|------|---------|-------------|
| BLE HID | `bt_hid` | Enable BLE HID keyboard mode on ESP32 |
| HID Type | `bt_hid_type` | Type text via BLE HID |
| MeshCore Messenger | Python-native | Fullscreen mesh chat with background reception |
| Flipper Zero | USB serial | SubGHz RX/TX, NFC read/emulate, signal replay |

### SYSTEM

| Item | Command | Description |
|------|---------|-------------|
| STOP ALL | `stop` | Emergency stop all operations |
| GPS | — | Toggle GPS module ON/OFF (AIO GPIO) |
| LoRa | — | Toggle LoRa module ON/OFF (AIO GPIO, auto-starts MeshCore) |
| Whitelist | — | Manage MAC whitelist — whitelisted devices are hidden from scans, attacks, and wardriving |
| Upload WPA-SEC | — | Upload all handshake .pcap files to wpa-sec.stanev.org (prompts for API key if not configured) |
| Download WPA-SEC | — | Download cracked passwords (potfile) from wpa-sec.stanev.org |
| Reboot ESP32 | `restart` | Restart ESP32 device |
| Download Map | — | Download OSM tiles (~10 km radius around current position) for offline street-level map. Press again to cancel. |
| Flash ESP32 | — | Download latest firmware from GitHub + flash via esptool. Board picker: WROOM / XIAO |

## Flipper Zero

Connect Flipper Zero via USB to access SubGHz and NFC features from within the game.

### SubGHz Toolkit
- **Signal Scanner (433/868 MHz)** — live monitoring on common frequencies
- **Signal Scanner (RAW)** — raw signal capture
- **Replay Signals** — browse folders on Flipper SD card, select and transmit `.sub` files
- **Flipper Chat** — SubGHz chat between Flippers (placeholder)

### NFC Toolkit
- **NFC Read Tag** — read full tag info (type, UID, ATQA, SAK, pages, NDEF, signature). Auto-saves to `loot/<session>/nfc/` with user-chosen name
- **NFC Scanner** — continuous NFC detection
- **NFC Emulate** — browse `.nfc` files on Flipper SD, select and emulate card

> Flipper auto-detects by USB VID:PID. If disconnected, the game shows a connection prompt.

### Flipper Controls

| Key | Action |
|-----|--------|
| `UP` / `DOWN` | Navigate menu / file list |
| `ENTER` | Select / transmit / emulate |
| `X` or `ESC` | Stop scanner / back |
| `PgUp` / `PgDn` | Scroll output log |

## XP & Progression

### Level System (20 ranks)

| Level | Title | XP Required |
|-------|-------|-------------|
| 1 | NOOB | 0 |
| 2 | SCRIPT_KIDDIE | 100 |
| 3 | SKIDDIE+ | 500 |
| 4 | WANNABE | 1,500 |
| 5 | PACKET_MONKEY | 3,000 |
| 6 | WARDRIVER | 6,000 |
| 7 | HACKER | 10,000 |
| 8 | NETRUNNER | 20,000 |
| 9 | PHREAKER | 35,000 |
| 10 | EXPLOIT_DEV | 50,000 |
| 11 | ELITE | 75,000 |
| 12 | SHADOW_OPS | 100,000 |
| 13 | GHOST | 150,000 |
| 14 | ZERO_DAY | 250,000 |
| 15 | APT_AGENT | 400,000 |
| 16 | CYBER_DEMON | 600,000 |
| 17 | CYBER_GOD | 1,000,000 |
| 18 | DIGITAL_DEITY | 2,500,000 |
| 19 | MATRIX_BREAKER | 5,000,000 |
| 20 | FINAL_BOSS | 10,000,000 |

### XP Rewards

| Action | New Device | Duplicate |
|--------|-----------|-----------|
| WiFi network scanned | 15 XP | 1 XP |
| BLE device detected | 10 XP | 1 XP |
| Handshake captured | 200 XP | — |
| Evil Twin credential | 150 XP | — |
| Evil Twin client | 25 XP | — |
| Device hacked (SPACE) | 50 XP | — |
| NFC tag read | 15 XP | — |
| NFC tag saved | 25 XP | — |
| Flipper TX signal | 25 XP | — |

XP persists across sessions via `loot_db.json`. Duplicate detection uses all-time loot history.

### Hacker Hat Profile

Dynamic classification based on ratio of attack vs recon activity:

| Hat | Color | Profile |
|-----|-------|---------|
| WHITE | White | Ethical recon — scanning & mapping only |
| BLUE | Blue | Blue Team — defensive security research |
| GREY | Grey | Grey Hat — mixed recon & offensive ops |
| RED | Red | Red Team — active penetration testing |
| BLACK | Black | Black Hat — aggressive attack operator |

### Achievement Badges

Persistent badges earned by milestones, saved to `loot_db.json`:

| Badge | Earned By |
|-------|-----------|
| FLIPPER | First Flipper Zero action |
| WARDRIVER | First WiFi/BLE wardriving session |
| MESHCORE | First MeshCore message received |
| HS HUNTER | First WPA handshake captured |
| WPA-SEC | First successful WPA-sec upload |
| EVIL TWIN | First credential captured via Evil Twin |

## Attack Details

### Dragon Drain (WPA3 SAE Flood)

Exploits CVE-2019-9494. Sends spoofed SAE Commit frames to overwhelm the target AP's elliptic curve computation, causing denial of service. Runs entirely on uConsole using scapy — does not use ESP32 serial. **Requires external WiFi adapter in monitor mode** (e.g., Alfa AWUS036ACH).

### MITM (ARP Spoofing)

Full man-in-the-middle attack with dedicated JanOS-style sub-screen:

1. **Idle** — attack description and info
2. **Interface selection** — auto-detect or pick from list
3. **Target mode** — single IP, scan subnet + select, or all devices
4. **Confirmation dialog** — shows victims, gateway, interface
5. **Running** — live scrolling log: DNS queries (cyan), HTTP requests (green), credentials (red)

Saves full pcap to `loot/<session>/mitm/`. Restores ARP tables on stop.

### BlueDucky (BLE HID Injection)

Exploits CVE-2023-45866 for unauthenticated Bluetooth HID pairing. Scans for BLE devices, pairs without user confirmation, and injects keystrokes. Includes Rick Roll payload.

### RACE Attack (Airoha BT Exploit)

Targets Airoha, Sony, and TRSPX Bluetooth SoCs (CVE-2025-20700/20701/20702). Extracts link keys and device info via GATT debug interface.

## MeshCore Messenger

Background mesh chat over LoRa SX1262 (869.618 MHz). Auto-starts with LoRa toggle in SYSTEM — sends advert to mesh network so messages can be received immediately. Closing the chat (ESC) keeps MeshCore running in background.

- **Fullscreen chat** with scrollable message history
- **Multi-channel support** — public, hashtag (#name), and private channels
- **Heard Nodes panel** — [H] shows discovered nodes with type, RSSI, SNR, age
- **Channel picker** — [C] to select active TX channel, [/] quick-switch
- **Speech bubbles** on map with CB radio sprite when messages arrive
- **Toast notifications** — always-on-top across all screens with sound
- **Node discovery** with GPS coordinates saved to loot (dedup by node ID)
- **Persistent config** — node name + channels saved to `~/.janos_meshcore.json`
- **Random node name** — auto-generated `WatchDogs-XXXXXX` on first run
- **LoRa HUD status** in bottom bar (in line with GPS info)

### Messenger Controls

| Key | Action |
|-----|--------|
| `A-Z`, `0-9` | Type message |
| `ENTER` | Send message on active channel |
| `A` | Send advert (broadcast presence) |
| `N` | Change node name (persistent) |
| `H` | Toggle Heard Nodes panel |
| `C` | Open channel picker |
| `[` / `]` | Quick-switch channels |
| `X` | Clear chat log |
| `PgUp` / `PgDn` | Scroll history |
| `ESC` | Back to map (MeshCore stays active) |

## LoRa Features

Requires SX1262 module on AIO v2 (`/dev/spidev1.0`).

| Feature | Frequencies | Description |
|---------|-------------|-------------|
| MeshCore Messenger | 869.618 MHz | Background mesh chat with auto-advert, speech bubbles on map |

## Map

- **Coastlines**: Natural Earth 50m data (~2200 points)
- **Tile rendering**: downloadable OSM map tiles for detailed street-level view
- **Map Downloader**: SYSTEM > Download Map fetches OpenStreetMap tiles in a ~10 km radius around current GPS position (or map center without fix). Progress shown in terminal. Press again to cancel. Tiles saved to `maps/` for offline use.
- **14 zoom levels**: from WORLD (360deg) to CLOSE-UP (0.02deg)
- **Markers**: green = WiFi loot, cyan = BT loot, red = handshake, yellow = MeshCore
- **Radar**: top-right corner, real-time device positions
- **GPS tracking**: auto-centers on live position when fix available

## GPS

Reads NMEA from `/dev/ttyAMA0` (AIO v2) or auto-detects USB GPS.

Status in bottom HUD:
- **Left**: LoRa ON/OFF status
- **Right**: GPS status — `Waiting for GPS fix` / `Waiting fix Vis:N` / `51.1234N 17.9876E`

Without GPS: arrow keys for manual pan.

## Loot

Saved to `loot/<session>/`:

| File | Content |
|------|---------|
| `serial_full.log` | Complete ESP32 serial output |
| `wardriving.csv` | WiGLE-format WiFi/BLE data + GPS |
| `bt_devices.csv` | BLE devices with GPS coordinates |
| `handshakes/` | PCAP, HCCAPX, .22000 (hashcat-ready) |
| `mitm/` | MITM pcap captures |
| `attack_events.log` | Attack start/stop/credential log |
| `meshcore_nodes.csv` | Discovered MeshCore nodes with GPS |
| `meshcore_messages.log` | MeshCore chat message history |
| `nfc/` | NFC tag dumps from Flipper Zero |
| `whitelist.json` | MAC address whitelist |

## Architecture

```
watchdogs/
  app.py              Main game loop, UI rendering, menu system
  serial_manager.py   ESP32 serial comm (115200 baud, USB auto-detect)
  gps_manager.py      NMEA parser (/dev/ttyAMA0 default)
  loot_manager.py     Loot saving (CSV, PCAP, handshakes)
  network_manager.py  WiFi scan result parsing
  app_state.py        Shared state (networks, GPS, BLE devices)
  config.py           Constants, ESP32 commands, API endpoints
  coastline.py        World coastline data (Natural Earth 50m)
  tile_manager.py     Map tile rendering + download
  dragon_drain.py     WPA3 SAE flood (scapy, standalone)
  mitm.py             ARP spoofing + tcpdump (standalone)
  bt_ducky.py         BLE HID injection (D-Bus, standalone)
  race_attack.py      Airoha BT exploit (bleak GATT)
  lora_manager.py     LoRa SX1262 (sniffer, MeshCore multi-channel)
  flipper_manager.py  Flipper Zero serial CLI (SubGHz, NFC, storage)
  aio_manager.py      AIO v2 GPIO control
  upload_manager.py   WPA-sec upload (pcap) + download (potfile)
  portals.py          Evil Twin/Portal HTML templates + upload
  convert_sprite.py   Sprite asset conversion (pyxel 16-color palette)
```

## Configuration Files

| File | Location | Purpose |
|------|----------|---------|
| `secrets.conf` | Project root | WPA-sec + WiGLE + WDGoWars API keys |
| `.watchdogs_meshcore.json` | `~/` | MeshCore node name + channels |
| `.watchdogs_meshcore_key` | `~/` | Ed25519 keypair for MeshCore signing |
| `loot_db.json` | `loot/` | Aggregate stats, XP, badges |
| `last_run.log` | `~/.watchdogs/` | Game log (rotated to `previous_run.log`) |

## Troubleshooting & Bug Reports

The game writes a full log to `~/.watchdogs/last_run.log` on every launch
(rotated to `previous_run.log` on next start). It contains:

- A clearly-marked `=== SESSION START ===` block with diagnostic info
  (OS, hardware model, Python version, detected USB serial devices,
  game version, display environment)
- All log messages from game subsystems (serial, GPS, LoRa, plugins)
- Full Python tracebacks for any unhandled exception, captured before
  the process dies

### Reporting a bug

The fastest way — let the game format the report for you:

```bash
sudo -u $USER python3 -m watchdogs --bugreport > /tmp/wdg-bug.md
cat /tmp/wdg-bug.md   # review it (no API keys, no GPS, just diagnostics)
```

Then open [a new issue](https://github.com/LOCOSP/esp32-watch-dogs/issues/new),
paste the contents of `wdg-bug.md`, and add:

1. **What you tried to do** (one sentence)
2. **What happened instead** (one sentence)
3. **Was the game running before the bug?** Yes / no / hard to say

You can also do it manually if you prefer — just open
`~/.watchdogs/last_run.log`, scroll to the most recent
`=== SESSION START — copy from here for bug reports ===` marker, copy
everything from that line to the end, and paste it into your issue
inside a triple-backtick code block.

### Common issues

**Game won't start, "ImportError: pyxel"** — re-run setup:
```bash
cd ~/python/esp32-watch-dogs && bash setup.sh
```

**"Permission denied" on /dev/ttyUSB0** — your user is not in the
`dialout` group:
```bash
sudo usermod -a -G dialout $USER
# log out and back in (or reboot)
```

**ESP32 not detected** — check `lsusb` for one of: CP2102, CH340,
FTDI, or Espressif USB-JTAG. The game logs all USB serial devices it
sees in the diagnostic block.

**MeshCore radio stays "OFF"** — make sure `meshtasticd` is not
holding the SPI bus:
```bash
sudo systemctl stop meshtasticd
sudo systemctl disable meshtasticd
```
The launcher does this automatically on every start, but only if the
service is installed.

**HTTPS errors when uploading to wdgwars.pl** — check `~/.watchdogs/last_run.log`
for SSL errors. Most often caused by an expired system CA bundle:
```bash
sudo apt-get install --reinstall ca-certificates
```

**"Invalid API key (401)"** when adding your wdgwars.pl key — copy
the key from your profile page on the portal exactly (64 hex chars,
no quotes, no spaces).

## Contributing

Pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
dev environment setup, coding style, and what currently needs help.
Bug reports go in [GitHub Issues](https://github.com/LOCOSP/esp32-watch-dogs/issues);
please use `python3 -m watchdogs --bugreport` to generate a paste-ready
diagnostic block (described above).

For the full list of what's working, what's WIP, and what's been fixed,
see [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) — use it, fork it, ship it. The license file contains an
additional notice about the legal responsibility of using offensive
security tooling against systems you don't own.

## Repository

- **GitHub** (primary): [github.com/LOCOSP/esp32-watch-dogs](https://github.com/LOCOSP/esp32-watch-dogs)
- **Landing page**: [locosp.org](https://locosp.org)
- **Community portal**: [wdgwars.pl](https://wdgwars.pl)
