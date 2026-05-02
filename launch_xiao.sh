#!/bin/bash
XIAO=$(ls /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_* 2>/dev/null | head -1)
if [ -z "$XIAO" ]; then
    echo "XIAO ESP32-C5 not found — plug it in and try again"
    read -p "Press Enter to close..."
    exit 1
fi
cd /home/fusedstamen/python/WatchDogsGo
exec sudo -E .venv/bin/python3 -m watchdogs "$XIAO"
