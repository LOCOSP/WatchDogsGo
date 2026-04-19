# Changelog

All notable changes to **Watch Dogs Go** are documented in this file.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).
This project follows [Semantic Versioning](https://semver.org/) — currently in
the `0.x` series, meaning the API and on-disk format may still change between
minor versions.

---

## [0.9.1] — 2026-04-19

Readability, performance and data-loss protection. No new gameplay.

### Added

#### Typography — Spleen 5x8 global font

- **`assets/spleen-5x8.bdf`** — [Spleen](https://github.com/fcambus/spleen) by
  Frederic Cambus, size 5x8, BSD-2-Clause, 76 KB BDF. Glyphs fit 95 ASCII
  codepoints; Polish letters are transliterated by the MeshCore chat path
  (`ą→a, ć→c, ę→e, ł→l, ń→n, ó→o, ś→s, ź→z, ż→z`, both cases) since BDF
  is ASCII-only and our Pyxel build doesn't support Unicode fonts.
- Loaded in `WatchDogsGame.__init__` as `self._font = pyxel.Font(path)`.
  The built-in pyxel 4x6 font is kept only as a fallback when the BDF
  fails to parse.
- **Applied globally** by monkey-patching `pyxel.text` right after the
  font loads:

  ```python
  _orig = pyxel.text
  _f = self._font
  def _patched_text(x, y, s, col, font=None, _o=_orig, _f=_f):
      _o(x, y, s, col, font if font is not None else _f)
  pyxel.text = _patched_text
  ```

  This covers ~370 call sites without touching them individually. Calls
  that pass a font explicitly (e.g. the messenger's legacy 8x8 `_big_font`
  for some labels) are untouched by the wrapper.
- Size trade: visible terminal lines drop from ~22 (4x6) to ~12 (5x8),
  but each line is actually legible on the uConsole panel.

#### Map markers

- **`_draw_markers` now branches on `MapMarker.type`**:
  - `"meshcore"` → 5x5 cyan diamond (`pyxel.rect` + 4 cardinal `pset`s)
    with 22/40 frames of an outer `circb` pulse ring at radius 6,
    label in cyan from zoom ≥ 4.
  - `"handshake"` → unchanged red skull shape (blinking ring at radius 4,
    body `rect(-2,-1,5,4)`, crown `rect(-1,-3,3,2)`, warning-colored
    centre pixel), label from zoom ≥ 5.
- **`_draw_aircraft`** replaced the old 3-pixel arrow with a 6x5 plus
  shape: `line(-3,0,+3,0)` wings, `pset(0,-2/-1/+1)` fuselage,
  `pset(±1,+2)` tail fins, plus a 14/40 frames `circb` pulse at radius 5.
  Altitude-colored (green < 5 kft, yellow < 20 kft, cyan above).
  Result: planes and nodes stay visible on globe/continent views where
  they previously collapsed to a single pixel under the coordinate
  quantization.

#### Power-cut resilience — event-driven fsync

- New module-level helpers in `watchdogs/loot_manager.py`:
  - `_fsync_file(fh)` — `fh.flush() + os.fsync(fh.fileno())`, wrapped
    in `try/except (OSError, ValueError)` so a concurrently-closed
    handle doesn't crash the call.
  - `_sync_append(path, text, encoding, newline)` — append wrapper.
  - `_sync_write(path, text, encoding, newline)` — truncating wrapper.
- Applied to every save path that produces **unrecoverable** user data:

  | File | Call site | Type |
  |---|---|---|
  | `portal_passwords.log` | `save_portal_event` | append |
  | `evil_twin_capture.log` | `save_evil_twin_event` | append |
  | `attacks.log` | `log_attack_event` | append |
  | `wardriving.csv` | `save_wardriving_network` / `save_wardriving_bt` | append + rewrite (on dedupe) |
  | `scan_results.csv` | `save_scan_results` | truncate |
  | `sniffer_aps.csv` | `save_sniffer_aps` | truncate |
  | `sniffer_probes.csv` | `save_sniffer_probes` | truncate |
  | `meshcore_nodes.csv` | `save_meshcore_node` | append |
  | `meshcore_messages.log` | `save_meshcore_message` | append |
  | `bt_devices.csv` | `save_bt_device` | append |
  | `bt_airtag.log` | `save_bt_airtag_event` | append |
  | `handshakes/*.txt` | `_save_handshake_txt` | truncate |
  | `handshakes/*.pcap` | `_save_pcap_stream` | binary truncate |
  | `handshakes/*.hccapx` | `_save_pcap_stream` | binary truncate |
  | `loot_db.json` | `_save_db` (tmp write before atomic rename) | truncate |

- Motivation: `with open(path, 'a') as fh: fh.write(...)` only pushes
  bytes into Python's FileIO buffer → `close()` flushes to the OS page
  cache → the kernel writes to the block device whenever it feels like
  it (on default ext4 / f2fs that's ~5–30 s via `dirty_writeback_centisecs`).
  A hard power cut anywhere in that window loses the data. `os.fsync`
  issues a FUA / cache-flush SCSI command and doesn't return until the
  block device confirms the write is on stable media.
- Cost on the uConsole's eMMC / Class 10 SD: ~5–20 ms per fsync call
  under typical load. Cheap per event (a captured password fires once),
  expensive per frame — so the serial log stream deliberately stays on
  `flush()` only (see next section).

#### Power-cut resilience — periodic background sync

- `LootManager.__init__` starts a `threading.Thread(name="loot-backup",
  daemon=True)` running `_periodic_backup_loop`.
- `BACKUP_INTERVAL = 30` seconds. The loop sleeps in 1-second
  increments so `close()` can flip `_backup_stop = True` and the thread
  exits within 1 s instead of potentially 30 s — important because
  `close()` then closes the serial log handle the thread was touching.
- Each pass calls `flush_all()`:
  1. `fsync` the open `serial_full.log` file handle (catches each ESP32
     line that was `flush()`'d but not yet `fsync`'d).
  2. `os.sync()` — a single syscall that schedules write-out of **all**
     dirty pages across every mounted filesystem. Handles stragglers
     from the tile cache, `loot_db.json` rewrites, plugin state files,
     log rotation, anything else.
- `os.sync()` latency on a Class 10 SD: 100 ms–2 s depending on
  backpressure. Running it off the main thread keeps the 30 FPS game
  loop untouched.
- `LootManager.close()` now stops the thread, runs a final
  `flush_all + os.sync`, writes the session summary (also fsync'd),
  updates `loot_db.json`, and only then closes `self._serial_fh`.

### Changed

#### UI relayout — from 4x6 to 5x8 metrics

- Most touches are mechanical: `len(text) * 4` → `len(text) * 5`,
  `y += 6` → `y += 8` (plus 1–2 px gap), row heights bumped 10 → 12
  or 13, vertical text centering offsets `(h - 6) // 2` → `(h - 8) // 2`.
- Selected concrete changes (file: `watchdogs/app.py`):

  | Component | Before | After |
  |---|---|---|
  | HUD top badge box | `rectb(x, 3, len(label)*4+3, 9)` | `rectb(x, 2, len(label)*5+4, HUD_TOP-4)` |
  | HUD bottom counter anchors | `4/52/110/146/190/240` | `4/60/125/170/220/270` |
  | Messages stack spacing | `y -= 8` | `y -= 10` |
  | Main menu tab height | 9 | 12 |
  | Main menu item height (`IH`) | 10 | 13 |
  | Input dialog field height | 16 | 20 |
  | Input dialog box width | 280 | 320 |
  | Confirm-quit dialog | 220x64 | 260x78 |
  | GPS-wait dialog | 260x58 | 280x70 |
  | Evil Twin picker | 480x220, rows 12 | 540x260, rows 14 |
  | Portal picker | 360x180 | 420x220 |
  | Cluster popup | 300 wide, rows 12 | 340 wide, rows 14 |
  | Captured-data overlay | 500x200, rows 10 | 560x240, rows 12 |
  | MeshCore toast | `len*6+60`, h=28 | `len*5+60`, h=32 |
  | MeshCore chat bubble | `len*4+8`, h=10 | `len*5+8`, h=12 |
  | Hacker-quip bubble | `len*4+16`, h=18 | `len*5+16`, h=20 |
  | Flipper Zero row height | 12 | 13, ASCII line 7→9 |
  | MITM row height | 10/12/14 mixed | 12 normalized |
  | MITM log line height | 7 | 9 |
  | Watch PIN overlay | 200x60 | 240x80 |
  | Loot screen column rows | `y += 9` | `y += 10` (`ROW_H` constant) |
  | Loot bottom list row height | 8 | 10 |
  | Whitelist row height | 10 | 12 |
  | Flash picker row height | 13 | 14 (`ROW_H` constant) |

#### MeshCore Messenger font

- `_draw_mc_screen` used to branch on `bf = self._big_font`, producing
  two parallel code paths with `line_h = 10 if bf else 6` and
  `char_w = 8 if bf else 4`. The branch is removed and the function
  now writes plain `pyxel.text(x, y, s, col)` calls that pick up the
  monkey-patched Spleen 5x8, with `line_h = 10`, `char_w = 5`.
- `self._big_font` itself is still loaded from `assets/font_8x8.bdf`
  for potential future use but is no longer passed to any `pyxel.text`
  call in the codebase.

### Performance

- **Cluster color / radius cache.** `_update_clusters` (throttled to
  ~1 Hz by `pyxel.frame_count - self._cluster_frame < 30` plus
  zoom/center change detection) now writes two extra fields on each
  cluster dict:

  ```python
  cl["color"] = C_HACK_CYAN if bt > wifi else C_SUCCESS
  cl["radius"] = min(5 + cl["count"] // 3, 12)
  ```

  `_draw_loot_points` reads `cl["color"]` / `cl["radius"]` instead of
  calling the former `_cluster_color(points)` per cluster per frame
  (which iterated `points` twice counting types).
- **Coastline bounding boxes.** `__init__` now builds
  `self._coast_bounds = [(min_lat, max_lat, min_lon, max_lon,
  antimerid), …]` one entry per segment in `COASTLINES`
  (~100 segments, ~2200 points). `_draw_coastlines` reads the precomputed
  bounds instead of rebuilding `lats = [p[0] for p in seg]` /
  `min()`/`max()` every frame, and the previous double loop that
  called `geo_to_screen` twice per point at zoom ≥ 5 (once for the
  line pass, once for the `pset` pass) was collapsed into a single
  pass.
- **Terminal color cache.** `_term_add` now computes the display color
  at append time via a new module-level `_color_for_terminal_line(line)`
  (which still does the 11 `startswith` / `in` checks), and stores it
  in a parallel `self._terminal_colors: list[int]` deque-trimmed to
  500 like `self.terminal_lines`. `_draw_terminal` indexes
  `colors_snap[i]` instead of re-evaluating all 11 checks per visible
  line per frame.

Combined, these three changes free roughly 10–20 ms of the 33 ms / 30 FPS
budget on the uConsole, measurably smoother when the map holds many loot
points and the terminal is actively streaming.

### Fixed

- **`loot_db.json` zero-byte window.** `_save_db` writes to
  `loot_db.tmp` then calls `Path.replace` which is atomic on the same
  filesystem, but the old code didn't fsync the tmp file before the
  rename — a crash between those two calls left a zero-byte
  `loot_db.json` next boot. The fix adds `_fsync_file(fh)` inside the
  tmp-write's `with` block so the on-disk bytes are durable before the
  rename flips the directory entry.
- **MeshCore nodes and ADS-B aircraft rendered identically** to
  handshake markers (a small red skull) because `_draw_markers` didn't
  branch on `MapMarker.type`. They now have the distinct shapes
  described in Added above.

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

[0.9.1]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/LOCOSP/WatchDogsGo/releases/tag/v0.9.0
