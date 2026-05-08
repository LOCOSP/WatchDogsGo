
## Bluetooth adapter pinning

hci enumeration order is non-deterministic on reboot. Pin adapters by BD address:

    /etc/udev/rules.d/99-bluetooth-stable.rules

    SUBSYSTEM=="bluetooth", KERNELS=="*uart*", ATTR{address}=="88:a2:9e:44:05:7f", NAME="hci0"
    SUBSYSTEM=="bluetooth", KERNELS=="*usb*", ATTR{address}=="38:7a:cc:84:a3:c8", NAME="hci1"

Bridge uses --bt-iface hci0 (CM4 internal UART, separate bus from WiFi).
