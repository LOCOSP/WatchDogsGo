#!/bin/bash
# Run ESP32 Watch Dogs
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

VENV_PYTHON=".venv/bin/python3"
if [ ! -f "$VENV_PYTHON" ]; then
    echo -e "${YELLOW}[run]${NC} No venv found. Running setup.sh first..."
    bash setup.sh || {
        echo -e "${RED}[run] setup.sh failed — fix errors above and re-run${NC}" >&2
        exit 1
    }
fi

# Verify venv still works after setup
if [ ! -x "$VENV_PYTHON" ]; then
    echo -e "${RED}[run] $VENV_PYTHON missing after setup — try: rm -rf .venv && bash setup.sh${NC}" >&2
    exit 1
fi

# Never create .pyc files — prevents root-owned cache blocking updates
export PYTHONDONTWRITEBYTECODE=1

# Suppress PulseAudio warnings when running as root
RUN_UID="${SUDO_UID:-$(id -u)}"
export XDG_RUNTIME_DIR="/run/user/${RUN_UID}"
export PULSE_SERVER="unix:/run/user/${RUN_UID}/pulse/native"

# Kill processes that hold SPI/GPIO for LoRa radio (SX1262) — only if installed
if command -v meshtasticd >/dev/null 2>&1; then
    sudo pkill -9 meshtasticd 2>/dev/null || true
    sudo systemctl stop meshtasticd 2>/dev/null || true
fi

# Clear any leftover bytecode cache (root-owned from previous runs)
sudo find "$SCRIPT_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Pre-flight: warn if user is not in dialout group (only when NOT running as root)
if [ "$(id -u)" -ne 0 ]; then
    if ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1 >/dev/null; then
        if ! id -nG "$USER" 2>/dev/null | grep -qwE 'dialout|tty|uucp'; then
            echo -e "${YELLOW}[run] Warning:${NC} user '$USER' is not in 'dialout' group"
            echo -e "       ESP32 serial access may require sudo. Fix permanently with:"
            echo -e "         ${GREEN}sudo usermod -a -G dialout $USER${NC}"
            echo -e "       Then log out and back in (or reboot)."
            echo ""
        fi
    fi
fi

# Wait for bridge PTY to be ready
for i in $(seq 1 10); do
    [ -e /tmp/esp32-pty ] && break
    sleep 0.5
done

exec sudo -E "$VENV_PYTHON" -m watchdogs "${@:-/tmp/esp32-pty}"
