# uConsole CM4 Setup Guide — WatchDogsGo Bridge

Setup guide for running WatchDogsGo on a ClockworkPi uConsole CM4 with
HackerGadgets AIO v1, no ESP32/C5 required. Uses `wdg_wifi_bridge.py` to
emulate the projectZero serial protocol over a PTY.

## Hardware

| Role | Hardware |
|------|----------|
| WiFi scanning (wardriving) | AC1200 (MT7921u) — internal USB-C via AIO board → `wlan1` |
| Internet / game uplink | CM4 internal Broadcom — `wlan0` |
| BLE scanning | CM4 internal UART Bluetooth — `hci0` (pinned via udev) |
| Handshake capture | AWUS036ACM (MT7612U) — external USB → `wlan2` |
| ADS-B / PiAware | AIO internal SDR (RTL2832U) |
| GPS | AIO internal GPS → gpsd → `/tmp/gps-pty` |

## System Dependencies

`iw` and `dump1090` are installed automatically by `setup.sh` (added in upstream 0.9.4).
Only install these manually if you are not using `setup.sh`:

```bash
sudo apt install -y airodump-ng hcxpcapngtool tshark
```

When tshark prompts about non-superuser capture — select **No**.
The bridge runs as root so non-superuser capture is not needed.

## Python Dependencies (venv)

```bash
cd ~/python/WatchDogsGo
.venv/bin/pip install bleak
```

Do NOT use `pip install --break-system-packages` — always use the venv pip.

## 1. Pin Bluetooth Adapters via udev

hci enumeration order is non-deterministic on reboot. Without pinning,
hci0 and hci1 swap randomly, breaking BLE scanning.

```bash
sudo nano /etc/udev/rules.d/99-bluetooth-stable.rules
```

```
# Pin CM4 internal UART BT to hci0
SUBSYSTEM=="bluetooth", KERNELS=="*uart*", ATTR{address}=="88:a2:9e:44:05:7f", NAME="hci0"

# Pin AC1200 USB BT to hci1
SUBSYSTEM=="bluetooth", KERNELS=="*usb*", ATTR{address}=="38:7a:cc:84:a3:c8", NAME="hci1"
```

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

After reboot: hci0 = CM4 UART BT (used by bridge), hci1 = AC1200 USB BT (unused).

> **Note:** Update the BD addresses if your hardware differs — check with `hciconfig`.

## 2. Unmanage wlan1 from NetworkManager

NetworkManager fights with `iw scan` on interfaces it manages, causing
scan timeouts and dropouts. Permanently unmanage wlan1:

```bash
sudo nano /etc/NetworkManager/conf.d/unmanaged.conf
```

```ini
[keyfile]
unmanaged-devices=interface-name:wlan1
```

```bash
sudo systemctl reload NetworkManager
```

wlan0 remains NM-managed and handles internet. wlan1 is dedicated to
wardriving scans and never associates to an AP.

## 3. Bring wlan1 Up on Boot

Since NM no longer manages wlan1, nothing brings it up after boot.
Add a systemd service:

```bash
sudo nano /etc/systemd/system/wlan1-up.service
```

```ini
[Unit]
Description=Bring up wlan1 for wardriving
After=network.target sys-subsystem-net-devices-wlan1.device
Wants=sys-subsystem-net-devices-wlan1.device

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 2
ExecStart=/sbin/ip link set wlan1 up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable wlan1-up
sudo systemctl start wlan1-up
```

## 4. Bridge Service

```bash
sudo nano /etc/systemd/system/wdg-wifi-bridge.service
```

```ini
[Unit]
Description=WatchDogsGo WiFi + BLE Bridge
After=network.target bluetooth.service wlan1-up.service
Wants=wlan1-up.service

[Service]
ExecStartPre=/usr/sbin/rfkill unblock bluetooth
ExecStartPre=/bin/bash -c 'bluetoothctl scan off 2>/dev/null; sleep 3'
ExecStart=/home/fusedstamen/python/WatchDogsGo/.venv/bin/python3 /home/fusedstamen/python/WatchDogsGo/wdg_wifi_bridge.py --iface wlan1 --bt-iface hci0 --sniffer-iface wlan2 --no-monitor --loot-dir /home/fusedstamen/python/WatchDogsGo/loot
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable wdg-wifi-bridge
sudo systemctl start wdg-wifi-bridge
```

## 5. GPS PTY Service

gpsd streams NMEA to the game via a socat PTY bridge:

```bash
sudo nano /etc/systemd/system/gps-socat.service
```

```ini
[Unit]
Description=gpsd to PTY bridge for WatchDogsGo
After=gpsd.service

[Service]
ExecStart=/bin/bash -c "gpspipe -r | socat - PTY,link=/tmp/gps-pty,raw,echo=0"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gps-socat
sudo systemctl start gps-socat
```

## 6. Launch the Game

```bash
cd ~/python/WatchDogsGo
sudo ./run.sh /tmp/esp32-pty
```

The bridge PTY is at `/tmp/esp32-pty`. `run.sh` waits up to 5 seconds
for it to appear before launching the game.

## Interface Summary

After boot, verify:

```bash
nmcli device status        # wlan1 should show 'unmanaged'
ip link show wlan1         # should show UP
iw dev wlan1 scan | grep -c BSS   # should return network count
hciconfig                  # hci0=UART, hci1=USB
systemctl status wdg-wifi-bridge.service  # should be active/running
```

## Handshake Capture

Place `hs_capture.py` in the WatchDogsGo directory alongside
`wdg_wifi_bridge.py`. The bridge dispatches to it automatically when
the game triggers `start_handshake`.

Requirements:
- AWUS036ACM plugged in and appearing as `wlan2`
- `airodump-ng`, `hcxpcapngtool`, `tshark` installed

The script manages its own monitor mode setup and teardown on `wlan2`.
Do not put `wlan2` in monitor mode manually while the bridge is running.

## Upstream Compatibility Notes

- **0.9.7** — `_looks_like_serial()` accepts any character device, so `/tmp/esp32-pty`
  works directly with `sudo ./run.sh /tmp/esp32-pty`. No patching required.
- **0.9.6** — wardrive plugin no longer marks the active session as uploaded during
  a concurrent upload, so handshake events fired by the bridge are not silently dropped.
- **0.9.4** — `setup.sh` installs `iw` and builds `dump1090` from source automatically.

## Troubleshooting

**BLE returns 0 devices instantly:** hci adapters have swapped. Check
`hciconfig` — if hci0 is USB and hci1 is UART the udev rule didn't apply.
Reload rules with `sudo udevadm control --reload-rules && sudo udevadm trigger`
then reboot.

**WiFi scan returns 0 networks:** Check `nmcli device status` — if wlan1
shows anything other than `unmanaged`, NM has taken it back. Re-apply
the unmanaged config and reload NM.

**wlan2 stuck in monitor mode after handshake:** Run:
```bash
sudo ip link set wlan2 down
sudo iw dev wlan2 set type managed
sudo ip link set wlan2 up
```

**Game loads slowly (minutes):** Normal with large loot datasets.
The game builds a cluster index for all scanned networks on startup.
142k+ WiFi records takes 2-3 minutes on CM4.
