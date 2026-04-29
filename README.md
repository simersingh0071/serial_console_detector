# serial_console_detector
auto serial console detector

Here's what the tool does and how to use it:

Features
Port Discovery

Scans all available serial ports using pyserial, with a fallback glob scan on Linux for ports pyserial might miss
Scores each port with a heuristic (USB-serial chips score highest, Bluetooth lowest) so the best candidate floats to the top

Baud Rate Probing (--probe)

Tries all common baud rates: 9600 → 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600
Sends a \r\n probe to wake the remote end, collects the response, and displays a rich results table

Device Fingerprinting

Matches received data against regex signatures for: Cisco IOS, Juniper JunOS, MikroTik, Linux, Arduino, ESP32/ESP8266, Raspberry Pi, U-Boot, OpenWRT, Windows

Auto-Detection (--auto)

Probes the top 3 candidate ports and picks the best (port, baud) pair automatically

Interactive Terminal

Full bidirectional terminal using raw tty mode (Unix) or msvcrt (Windows)
Press Ctrl-] to disconnect


Installation & Usage
bash

pip install pyserial rich

# List all detected ports
python serial_console_detector.py --list

# Auto-detect port and baud rate, then connect
python serial_console_detector.py --auto

# Probe baud rates on a specific port
python serial_console_detector.py --probe --port /dev/ttyUSB0

# Connect directly to a known port
python serial_console_detector.py --port COM3 --baud 115200 --connect

# Interactive menu (no flags)
python serial_console_detector.py

Linux tip: If you get permission errors, run sudo usermod -aG dialout $USER and log out/in.
