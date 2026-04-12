# Changelog

All notable changes to **Watch Dogs Go** are documented in this file.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).
This project follows [Semantic Versioning](https://semver.org/) — currently in
the `0.x` series, meaning the API and on-disk format may still change between
minor versions.

---

## [0.9.0] — 2026-04-12 (early access release)

First public release. The core gameplay loop is complete and stable. Several
advanced attacks are present in the menu but marked **(WIP)** because they
need rework before they're safe for general users.

### Added

- **Watch Dogs Go Wars Sync plugin** with full ecosystem integration:
  - Upload wardriving sessions to community server (wdgwars.pl)
  - Pull identity, stats and badges from `GET /api/me` after entering API key
  - Auto-rename LoRa MeshCore node to `WDG_<username>` so other players see you
  - Auto-validate API key, show "Invalid key" inline
  - 8 game badges synchronized two-way with the server
  - Level gate: locked until **Lv.6 WARDRIVER** (6000 XP) to avoid spam
  - Obfuscated default endpoint (URL is built into the plugin, user only
    enters their API key)
- **JanOS Loot Import plugin** — migrate old JanOS session folders into the
  game with automatic XP recalculation
- **PipBoy Watch (T-Watch Ultra)** support over BLE (NUS) — read NFC,
  send/receive LoRa MeshCore, control ESP32 attacks from the watch
- **Bruce Firmware compatibility** — accepts wardriving CSVs uploaded from
  any of 50+ ESP32 boards running Bruce
- **First-run experience**:
  - One-line installer (`curl -sL https://locosp.github.io/WatchDogsGo/install | sudo bash`)
  - `setup.sh` auto-installs Python deps, SDL2, BlueZ, tcpdump, aircrack-ng,
    rtl_433, dump1090, RPi5/CM5 GPIO library
  - `secrets.conf.example` template with documented API keys
  - Desktop launcher `.desktop` file
  - First-launch warning if user is not in `dialout` group
- **File logging** to `~/.watchdogs/last_run.log` (rotated to `previous_run.log`)
  with full unhandled-exception capture for bug reports
- **HUD shortcuts hint** — opening MeshCore Messenger now shows your unique
  node name and the available `Ctrl+N`/`Ctrl+A`/`Ctrl+C`/`Ctrl+X` shortcuts
- **Plugin system** that supports multiple plugins with overlay UIs

### Changed

- **Renamed all `janos_*` files and `JANOS_*` env vars to `watchdogs_*` /
  `WDG_*`** with full backwards compatibility:
  - `~/.janos_meshcore_key` → `~/.watchdogs_meshcore_key` (auto-migrated)
  - `~/.janos_meshcore.json` → `~/.watchdogs_meshcore.json` (auto-migrated)
  - `JANOS_WPASEC_KEY` / `JANOS_WIGLE_*` / `JANOS_GPS_*` / `JANOS_SOUND` —
    both old and new names accepted on read; new name used when writing
- **WiGLE CSV header** identifies as `appRelease=WatchDogsGo` (was `JanOS`)
- **Auto-update URL** points to `esp32-watch-dogs` repository
- **XIAO ESP32-C5 flash** uses `--before usb-reset` so firmware updates work
  from the in-game menu without holding the BOOT button (the XIAO module
  inside the uConsole has no accessible buttons)
- **Plugin command dispatch** now encodes plugin index in the command key
  (`_p_<idx>_<action>`) so multiple plugins with the same action name can
  coexist
- **MeshCore node names** are now generated from the user's Ed25519 public
  key on first run (`WatchDogs_xxxxxxxx` — 8 hex chars), and replaced by
  `WDG_<username>` after the wdgwars.pl API key is set

### Fixed

- **Upload 403** caused by missing `/api/upload/` path on the configured
  server URL
- **HTTPS on `wdgwars.pl`**: switched Traefik from TLS-ALPN-01 to HTTP-01
  challenge, fixed `acme.json` permissions, removed stale failed cert
  entries, fixed read-only SQLite database in the wardrive container
- **CSV parser crash** on MeshCore node names containing commas
  (e.g. `h,1Prz3`) — switched from `line.split(",")` to `csv.reader` and
  `csv.writer` everywhere
- **Every fresh user broadcasting as literal `WatchDogs`** on the LoRa
  mesh — the buggy default never let the unique-name generator run
- **Plugin overlays not opening** because `janos_import` and
  `wardrive_upload` shared the action name `open_overlay` and dispatch
  picked the first plugin alphabetically (which lacked a draw hook)
- **`get_local_time(timeout=5000)` blocking the LVGL UI** for 5 s on the
  PipBoy Watch firmware (handled in the watch repo, listed here for
  visibility because it affected the in-game watch screen)

### Disabled (work in progress)

These features are present in the menu but show a "[FEATURE] disabled — work
in progress" message when activated. They will return in a future update.

- **Download Map** (SYSTEM tab) — needs new tile source and resume support
- **BLE HID** (ADDONS tab) — Bluez D-Bus stack needs rewrite
- **HID Type** (ADDONS tab) — depends on BLE HID
- **BlueDucky** (ATTACK tab) — Bluetooth pairing race conditions
- **RACE Attack** (ATTACK tab) — Airoha BT exploit needs hardening

### Known limitations

- **Linux only** (Debian / Raspberry Pi OS / Ubuntu). macOS, Windows,
  Fedora, Alpine are not supported by the installer.
- **Game requires sudo** for raw socket access, GPIO and serial ports.
- **uConsole-first design** — runs on other Linux systems but the UI
  is sized for the 640x360 uConsole display.
- **Single-instance only** — running two copies of the game on the same
  machine will fight over the ESP32 serial port.

---

## [0.x history] — pre-release

The project evolved from **JanOS**, a terminal-based wardriving app for the
ClockworkPi uConsole. The first commit of the Pyxel-based "Watch Dogs Go"
frontend lands in early 2026; everything before that was JanOS work and is
not tracked in this changelog.

The major pre-release milestones were:

- **JanOS → Watch Dogs Go rewrite** (Pyxel UI, RPG progression, badges)
- **projectZero firmware** for ESP32-C5 (replaces JanOS firmware)
- **PipBoy-3000 firmware** for T-Watch Ultra
- **wdgwars.pl portal** (Django → PHP rewrite, gang warfare, anti-cheat,
  WiGLE-compatible upload, badge system)
- **Bruce Firmware integration** — pull request to upstream
  `BruceDevices/firmware` adding native upload to wdgwars.pl

[0.9.0]: https://github.com/LOCOSP/esp32-watch-dogs/releases/tag/v0.9.0
