#!/bin/bash
# =============================================================================
# ESP32 Watch Dogs — Full Setup Script
# Creates venv, installs all dependencies, checks system requirements.
# Usage: ./setup.sh  (or called automatically by run.sh)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC} $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }
info() { echo -e "  ${CYAN}[..]${NC} $1"; }

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  ESP32 Watch Dogs — Setup${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

ERRORS=0

# --- 1. Python 3 ---
echo "[1/7] Checking Python..."
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    ok "Found $PY_VER"
else
    fail "python3 not found! Install Python 3.10+"
    ERRORS=$((ERRORS + 1))
fi

# --- 2. venv module ---
echo "[2/7] Checking venv module..."
if python3 -c "import venv" 2>/dev/null; then
    ok "venv available"
else
    warn "venv not available — trying to install..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-venv 2>/dev/null && ok "Installed python3-venv" || fail "Could not install python3-venv"
    else
        fail "Install python3-venv manually"
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- 3. Create/update venv ---
echo "[3/7] Setting up virtual environment..."
if [ ! -d ".venv" ]; then
    info "Creating .venv..."
    python3 -m venv .venv 2>/dev/null || python3 -m venv .venv --without-pip
    ok "Created .venv"
else
    ok ".venv exists"
fi

# Bootstrap pip if missing
if [ ! -f ".venv/bin/pip" ] && [ ! -f ".venv/bin/pip3" ]; then
    info "Bootstrapping pip..."
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python3 /tmp/get-pip.py --quiet
    rm -f /tmp/get-pip.py
    ok "pip installed"
fi

# --- 4. Install Python dependencies ---
echo "[4/7] Installing Python packages..."
.venv/bin/pip install --upgrade pip --quiet 2>/dev/null
.venv/bin/pip install -r requirements.txt --quiet 2>/dev/null
ok "pip install complete"

# Link system rpi-lgpio into venv (RPi.GPIO shim for LoRaRF on newer SoCs)
# pip install LoRaRF pulls old RPi.GPIO 0.7.1 which doesn't know CM5.
# System python3-rpi-lgpio works — always force symlink AFTER pip install.
# CM4 on Bullseye: old RPi.GPIO works fine, no symlink needed.
# CM5 / RPi5 on Bookworm/Trixie: MUST use rpi-lgpio.
if [[ "$(uname)" == "Linux" ]]; then
    # Install rpi-lgpio if not present (needed for RPi5/CM5)
    if ! python3 -c "import rpi_lgpio" 2>/dev/null; then
        if command -v apt-get &>/dev/null; then
            info "Installing python3-rpi-lgpio (RPi5/CM5 GPIO support)..."
            sudo apt-get install -y python3-rpi-lgpio python3-lgpio 2>/dev/null || true
        fi
    fi

    if [ -d "/usr/lib/python3/dist-packages/RPi" ] && \
       [ -f "/usr/lib/python3/dist-packages/RPi/GPIO/__init__.py" ]; then
        VENV_SP=".venv/lib/python3.*/site-packages"
        for sp in $VENV_SP; do
            if [ -d "$sp" ]; then
                # Force remove pip-installed RPi.GPIO and replace with system rpi-lgpio
                rm -rf "$sp/RPi" "$sp/RPi.GPIO"* 2>/dev/null
                ln -sf /usr/lib/python3/dist-packages/RPi "$sp/RPi"
                ln -sf /usr/lib/python3/dist-packages/lgpio.py "$sp/lgpio.py" 2>/dev/null
                for so in /usr/lib/python3/dist-packages/_lgpio*.so; do
                    [ -f "$so" ] && ln -sf "$so" "$sp/$(basename $so)"
                done
                ok "rpi-lgpio force-linked into venv (LoRa GPIO support)"
                break
            fi
        done
    else
        info "System rpi-lgpio not available — using pip RPi.GPIO (OK for CM4/RPi4)"
    fi

    # Link system python3-gi (gi.repository.GLib) into venv. Same rationale
    # as rpi-lgpio above — system package has the native .so bindings that
    # pip can't easily rebuild, and our venv doesn't use
    # --system-site-packages.
    if [ -d "/usr/lib/python3/dist-packages/gi" ]; then
        VENV_SP=".venv/lib/python3.*/site-packages"
        for sp in $VENV_SP; do
            if [ -d "$sp" ] && [ ! -e "$sp/gi" ]; then
                ln -sf /usr/lib/python3/dist-packages/gi "$sp/gi"
                # _gi / _gi_cairo C extensions — PyGObject's native bindings
                for so in /usr/lib/python3/dist-packages/_gi*.so \
                          /usr/lib/python3/dist-packages/_gi_cairo*.so; do
                    [ -f "$so" ] && ln -sf "$so" "$sp/$(basename $so)"
                done
                # pygobject_* helper installed alongside gi in some distros
                for extra in pygobject_compat.py; do
                    f="/usr/lib/python3/dist-packages/$extra"
                    [ -f "$f" ] && ln -sf "$f" "$sp/$extra"
                done
                ok "python3-gi linked into venv (BlueZ pairing agent)"
                break
            fi
        done
    fi
fi

# Verify critical imports (required)
MISSING_REQ=""
.venv/bin/python3 -c "import pyxel" 2>/dev/null || MISSING_REQ="$MISSING_REQ pyxel"
.venv/bin/python3 -c "import serial" 2>/dev/null || MISSING_REQ="$MISSING_REQ pyserial"
.venv/bin/python3 -c "from PIL import Image" 2>/dev/null || MISSING_REQ="$MISSING_REQ Pillow"

if [ -z "$MISSING_REQ" ]; then
    ok "Required Python imports verified"
else
    fail "Missing required packages:$MISSING_REQ"
    ERRORS=$((ERRORS + 1))
fi

# Verify optional imports (advanced attacks + LoRa)
MISSING_OPT=""
.venv/bin/python3 -c "import scapy" 2>/dev/null || MISSING_OPT="$MISSING_OPT scapy"
.venv/bin/python3 -c "import netifaces" 2>/dev/null || MISSING_OPT="$MISSING_OPT netifaces"
.venv/bin/python3 -c "import bleak" 2>/dev/null || MISSING_OPT="$MISSING_OPT bleak"
.venv/bin/python3 -c "import dbus" 2>/dev/null || MISSING_OPT="$MISSING_OPT dbus-python"
.venv/bin/python3 -c "from gi.repository import GLib" 2>/dev/null || \
    MISSING_OPT="$MISSING_OPT python3-gi(apt)"
.venv/bin/python3 -c "import LoRaRF" 2>/dev/null || MISSING_OPT="$MISSING_OPT LoRaRF"
.venv/bin/python3 -c "import nacl" 2>/dev/null || MISSING_OPT="$MISSING_OPT PyNaCl"

if [ -z "$MISSING_OPT" ]; then
    ok "Optional Python imports verified (all attacks available)"
else
    warn "Optional packages not available:$MISSING_OPT"
    warn "Some attacks may not work (MITM, Dragon Drain, BlueDucky, RACE, LoRa)"
fi

# --- 5. System libraries (SDL2 for pyxel) ---
echo "[5/7] Checking system libraries..."
if command -v apt-get &>/dev/null; then
    # Debian/Ubuntu/Raspberry Pi
    PKGS_NEEDED=""
    dpkg -s libsdl2-dev &>/dev/null 2>&1 || PKGS_NEEDED="$PKGS_NEEDED libsdl2-dev"
    dpkg -s libsdl2-image-dev &>/dev/null 2>&1 || PKGS_NEEDED="$PKGS_NEEDED libsdl2-image-dev"

    if [ -n "$PKGS_NEEDED" ]; then
        info "Installing:$PKGS_NEEDED"
        sudo apt-get install -y $PKGS_NEEDED 2>/dev/null && ok "System libs installed" || warn "Could not install system libs (game may still work)"
    else
        ok "SDL2 libraries present"
    fi
elif [[ "$(uname)" == "Darwin" ]]; then
    ok "macOS — SDL2 bundled with pyxel"
else
    warn "Unknown platform — ensure SDL2 is installed"
fi

# --- 6. System tools (for advanced attacks) ---
echo "[6/7] Checking system tools..."
if command -v apt-get &>/dev/null; then
    SYS_NEEDED=""
    command -v tcpdump &>/dev/null || SYS_NEEDED="$SYS_NEEDED tcpdump"
    command -v airmon-ng &>/dev/null || SYS_NEEDED="$SYS_NEEDED aircrack-ng"
    # `iw` is used by Dragon Drain (monitor mode check) and serial_manager
    # (bridge interface detection). Preinstalled on Raspbian/RPi OS but not
    # on a clean Debian/Ubuntu server, so request it explicitly.
    command -v iw &>/dev/null || SYS_NEEDED="$SYS_NEEDED iw"
    command -v rtl_433 &>/dev/null || SYS_NEEDED="$SYS_NEEDED rtl-433"
    command -v hciconfig &>/dev/null || SYS_NEEDED="$SYS_NEEDED bluez bluez-tools"
    command -v pactl &>/dev/null || SYS_NEEDED="$SYS_NEEDED pulseaudio-utils"
    # pinctrl is used by AIO v2 GPIO toggles (GPS/LoRa/SDR/USB power rails)
    command -v pinctrl &>/dev/null || SYS_NEEDED="$SYS_NEEDED raspi-utils"
    # python3-gi provides gi.repository.GLib, required by the BlueZ pairing
    # agent for the PipBoy watch. It's distributed as a system package (can't
    # go in requirements.txt — PyGObject on PyPI needs libgirepository1.0-dev
    # + gobject-introspection anyway, same native deps). Symlinked into the
    # game's venv a few lines below, same pattern as rpi-lgpio.
    python3 -c "from gi.repository import GLib" 2>/dev/null || \
        SYS_NEEDED="$SYS_NEEDED python3-gi gir1.2-glib-2.0"

    if [ -n "$SYS_NEEDED" ]; then
        info "Installing:$SYS_NEEDED"
        sudo apt-get install -y $SYS_NEEDED 2>/dev/null && ok "System tools installed" || warn "Could not install:$SYS_NEEDED"
    else
        ok "All system tools present (tcpdump, aircrack-ng, iw, rtl_433, bluez, pulseaudio-utils, pinctrl)"
    fi

    # --- dump1090 (FlightAware fork) -----------------------------------------
    # The classic `dump1090-mutability` apt package has been an archived
    # upstream since 2018 (no fixes, no RTL-SDR v4 support). Build the
    # actively-maintained FlightAware fork from source — the binary still
    # installs as `dump1090`, so the game's `shutil.which("dump1090")` and
    # the `dump1090 --net --quiet` subprocess call work unchanged.
    if ! command -v dump1090 &>/dev/null; then
        info "dump1090 not found — building FlightAware fork from source..."
        sudo apt-get install -y librtlsdr-dev pkg-config build-essential git \
            2>/dev/null || warn "Could not install dump1090 build deps"
        TMP=$(mktemp -d)
        if git clone --depth=1 https://github.com/flightaware/dump1090.git \
                "$TMP/dump1090" >/dev/null 2>&1; then
            if (cd "$TMP/dump1090" && make -j"$(nproc)" >/dev/null 2>&1 \
                    && sudo cp dump1090 /usr/local/bin/); then
                ok "dump1090 (FlightAware) built and installed to /usr/local/bin"
            else
                warn "dump1090 build failed — ADS-B radar will be disabled"
            fi
        else
            warn "Could not clone flightaware/dump1090 — ADS-B radar disabled"
        fi
        rm -rf "$TMP" 2>/dev/null
    else
        ok "dump1090 present ($(command -v dump1090))"
    fi

    # AIO v2 control (uConsole only — optional, install only if pinctrl exists
    # AND the user is on a Raspberry Pi-class device).
    # Official install method: https://github.com/hackergadgets/aiov2_ctl/
    #   git clone + sudo python3 ./aiov2_ctl.py --install
    if command -v pinctrl &>/dev/null && [ -f /sys/firmware/devicetree/base/model ]; then
        if ! command -v aiov2_ctl &>/dev/null; then
            info "Installing aiov2_ctl from GitHub (uConsole AIO v2 hardware control)..."
            sudo apt-get install -y python3-pyqt6 git 2>/dev/null || true
            TMP=$(mktemp -d)
            if git clone --depth=1 https://github.com/hackergadgets/aiov2_ctl.git "$TMP/aiov2_ctl" 2>/dev/null; then
                if (cd "$TMP/aiov2_ctl" && sudo python3 ./aiov2_ctl.py --install >/dev/null 2>&1); then
                    ok "aiov2_ctl installed"
                else
                    warn "aiov2_ctl installer failed — AIO toggles will be disabled"
                fi
            else
                warn "Could not clone aiov2_ctl repo — AIO toggles will be disabled"
            fi
            rm -rf "$TMP" 2>/dev/null
        else
            ok "aiov2_ctl present (AIO v2 control available)"
        fi
    fi
else
    # Check existence only
    TOOLS_FOUND=""
    command -v tcpdump &>/dev/null && TOOLS_FOUND="$TOOLS_FOUND tcpdump"
    command -v airmon-ng &>/dev/null && TOOLS_FOUND="$TOOLS_FOUND aircrack-ng"
    command -v dump1090 &>/dev/null && TOOLS_FOUND="$TOOLS_FOUND dump1090"
    command -v rtl_433 &>/dev/null && TOOLS_FOUND="$TOOLS_FOUND rtl_433"
    command -v hciconfig &>/dev/null && TOOLS_FOUND="$TOOLS_FOUND hciconfig"
    if [ -n "$TOOLS_FOUND" ]; then
        ok "Found:$TOOLS_FOUND"
    else
        warn "System tools not found — install manually for full feature support"
    fi
fi

# --- 7. Permissions & files ---
echo "[7/7] Checking permissions..."
[ -f "run.sh" ] && chmod +x run.sh
[ -f "setup.sh" ] && chmod +x setup.sh
[ -f "watchdogs-launcher" ] && chmod +x watchdogs-launcher
ok "Scripts executable"

# Check serial access (Linux)
if [[ "$(uname)" == "Linux" ]]; then
    if groups | grep -qE '(dialout|tty)'; then
        ok "User in dialout/tty group (serial access)"
    else
        warn "User not in dialout group — ESP32 serial may need sudo"
    fi
fi

# Create runtime directories
mkdir -p loot maps plugins firmware_cache
ok "Data directories ready (loot, maps, plugins, firmware_cache)"

# Create secrets.conf from template if missing
if [ ! -f "secrets.conf" ] && [ -f "secrets.conf.example" ]; then
    cp secrets.conf.example secrets.conf
    chmod 600 secrets.conf 2>/dev/null || true
    ok "secrets.conf created from template (edit to add API keys)"
fi

# --- Summary ---
echo ""
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Setup complete! No errors.${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "  Run the game:  sudo ./run.sh"
    echo ""
else
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  Setup finished with $ERRORS error(s)${NC}"
    echo -e "${RED}========================================${NC}"
    echo ""
    echo "  Fix the errors above, then run setup.sh again."
    echo ""
    exit 1
fi
