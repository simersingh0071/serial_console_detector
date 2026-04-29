#!/usr/bin/env python3
"""
Serial Console Auto-Detection Tool
====================================
Automatically detects, identifies, and connects to serial console ports.
Supports baud rate probing, device fingerprinting, and interactive session.

Dependencies:
    pip install pyserial rich

Usage:
    python serial_console_detector.py              # Scan and list ports
    python serial_console_detector.py --auto       # Auto-detect & connect
    python serial_console_detector.py --port COM3  # Connect to specific port
    python serial_console_detector.py --probe      # Probe all baud rates
"""

import sys
import os
import time
import threading
import argparse
import platform
import glob
import re
from dataclasses import dataclass, field
from typing import Optional

# ─── Dependency check ────────────────────────────────────────────────────────

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("[ERROR] pyserial not found. Install it with: pip install pyserial")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import box
    from rich.text import Text
    from rich.live import Live
    from rich.layout import Layout
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("[WARNING] rich not found. Install it with: pip install rich")
    print("          Falling back to plain text output.\n")


# ─── Constants ────────────────────────────────────────────────────────────────

COMMON_BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

DEVICE_SIGNATURES = {
    "Cisco IOS":         [r"Press RETURN to get started", r"User Access Verification", r"cisco"],
    "Juniper JunOS":     [r"login:", r"Amnesiac"],
    "MikroTik RouterOS": [r"MikroTik", r"RouterOS"],
    "Linux Shell":       [r"login:", r"bash", r"#\s*$", r"\$\s*$"],
    "Arduino":           [r"Arduino", r"avr"],
    "ESP32/ESP8266":     [r"ets Jan", r"rst:0x", r"Guru Meditation"],
    "Raspberry Pi":      [r"raspberrypi login:", r"Raspberry Pi"],
    "U-Boot Bootloader": [r"U-Boot", r"Hit any key to stop autoboot"],
    "OpenWRT":           [r"OpenWrt", r"BusyBox"],
    "Windows Serial":    [r"Microsoft Windows", r"C:\\>"],
}

PROBE_TIMEOUT    = 1.5   # seconds to wait for response during baud probing
CONNECT_TIMEOUT  = 2.0   # seconds for initial connection
READ_CHUNK       = 4096  # bytes per read


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class PortInfo:
    device:       str
    description:  str  = "Unknown"
    hwid:         str  = ""
    vid:          Optional[int] = None
    pid:          Optional[int] = None
    manufacturer: Optional[str] = None
    product:      Optional[str] = None
    serial_number: Optional[str] = None
    detected_baud: Optional[int] = None
    detected_device: Optional[str] = None
    score:        int  = 0          # confidence score for auto-selection


@dataclass
class ProbeResult:
    port:        str
    baud:        int
    success:     bool
    data:        bytes = field(default_factory=bytes)
    device_type: str  = "Unknown"
    latency_ms:  float = 0.0


# ─── Console helper ───────────────────────────────────────────────────────────

console = Console() if RICH_AVAILABLE else None


def cprint(msg: str, style: str = "") -> None:
    if console:
        console.print(msg, style=style)
    else:
        print(msg)


def cerror(msg: str) -> None:
    cprint(f"[ERROR] {msg}", style="bold red")


def cinfo(msg: str) -> None:
    cprint(f"[INFO]  {msg}", style="cyan")


def csuccess(msg: str) -> None:
    cprint(f"[OK]    {msg}", style="bold green")


# ─── Port Discovery ───────────────────────────────────────────────────────────

def discover_ports() -> list[PortInfo]:
    """Return all available serial ports with metadata."""
    ports: list[PortInfo] = []
    raw_ports = list(serial.tools.list_ports.comports())

    # Fallback glob scan for systems where pyserial misses ports
    if platform.system() == "Linux":
        patterns = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*",
                    "/dev/ttyAMA*", "/dev/ttyO*"]
        found_devices = {p.device for p in raw_ports}
        for pat in patterns:
            for dev in sorted(glob.glob(pat)):
                if dev not in found_devices:
                    # Quick open test to see if port is usable
                    try:
                        s = serial.Serial(dev, timeout=0.1)
                        s.close()
                        raw_ports.append(
                            type("FakePort", (), {
                                "device": dev, "description": "Serial Port",
                                "hwid": "", "vid": None, "pid": None,
                                "manufacturer": None, "product": None,
                                "serial_number": None,
                            })()
                        )
                    except Exception:
                        pass

    for p in raw_ports:
        info = PortInfo(
            device=p.device,
            description=p.description or "Unknown",
            hwid=p.hwid or "",
            vid=getattr(p, "vid", None),
            pid=getattr(p, "pid", None),
            manufacturer=getattr(p, "manufacturer", None),
            product=getattr(p, "product", None),
            serial_number=getattr(p, "serial_number", None),
        )
        info.score = _score_port(info)
        ports.append(info)

    ports.sort(key=lambda x: x.score, reverse=True)
    return ports


def _score_port(info: PortInfo) -> int:
    """Heuristic score — higher = more likely a real console port."""
    score = 0
    desc_lower = (info.description or "").lower()
    hwid_lower  = (info.hwid or "").lower()

    # USB serial adapters are almost always intentional
    if "usb" in desc_lower or "usb" in hwid_lower:
        score += 30
    # Common USB-serial chip names
    for chip in ["ch340", "cp210", "ft232", "pl2303", "ch341", "silabs"]:
        if chip in desc_lower or chip in hwid_lower:
            score += 20
            break
    # Platform-specific preferred devices
    if platform.system() == "Linux":
        if "ttyUSB" in info.device:
            score += 25
        elif "ttyACM" in info.device:
            score += 20
    elif platform.system() == "Darwin":
        if "usbserial" in info.device.lower() or "usbmodem" in info.device.lower():
            score += 25
    elif platform.system() == "Windows":
        m = re.search(r"COM(\d+)", info.device)
        if m and int(m.group(1)) > 2:   # COM1/COM2 are legacy built-ins
            score += 15
    # Penalise built-in/Bluetooth ports
    for noise in ["bluetooth", "irda", "modem"]:
        if noise in desc_lower:
            score -= 20
    return score


# ─── Display ──────────────────────────────────────────────────────────────────

def display_ports(ports: list[PortInfo]) -> None:
    if not ports:
        cprint("\n[bold yellow]No serial ports found.[/bold yellow]")
        cprint("  • Check that your device is plugged in.")
        cprint("  • On Linux, you may need: sudo usermod -aG dialout $USER")
        return

    if console and RICH_AVAILABLE:
        tbl = Table(
            title="[bold cyan]Detected Serial Ports[/bold cyan]",
            box=box.ROUNDED,
            show_lines=True,
            highlight=True,
        )
        tbl.add_column("#",            style="bold white", width=4, justify="right")
        tbl.add_column("Port",         style="bold cyan",  min_width=12)
        tbl.add_column("Description",  style="white",      min_width=22)
        tbl.add_column("Manufacturer", style="yellow",     min_width=16)
        tbl.add_column("VID:PID",      style="magenta",    width=12)
        tbl.add_column("Score",        style="green",      width=7, justify="right")
        tbl.add_column("Likely Device",style="cyan",       min_width=14)

        for i, p in enumerate(ports, 1):
            vid_pid = (f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else "—")
            tbl.add_row(
                str(i),
                p.device,
                p.description,
                p.manufacturer or "—",
                vid_pid,
                str(p.score),
                p.detected_device or "—",
            )
        console.print()
        console.print(tbl)
    else:
        print(f"\n{'#':<4} {'Port':<15} {'Description':<30} {'Score':<6}")
        print("-" * 60)
        for i, p in enumerate(ports, 1):
            print(f"{i:<4} {p.device:<15} {p.description:<30} {p.score:<6}")


# ─── Baud-Rate Probing ────────────────────────────────────────────────────────

def probe_baud_rates(
    port: str,
    baud_rates: list[int] = COMMON_BAUD_RATES,
    send_probe: bool = True,
) -> list[ProbeResult]:
    """
    Try each baud rate; collect any data that arrives.
    Returns results sorted by likelihood of being correct.
    """
    results: list[ProbeResult] = []

    if console and RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as prog:
            task = prog.add_task(f"Probing {port} …", total=len(baud_rates))
            for baud in baud_rates:
                result = _try_baud(port, baud, send_probe)
                results.append(result)
                prog.advance(task)
    else:
        print(f"\nProbing {port}:")
        for baud in baud_rates:
            result = _try_baud(port, baud, send_probe)
            results.append(result)
            status = "OK" if result.success else "  "
            print(f"  {baud:>8} bps  [{status}]  {result.device_type}")

    results.sort(key=lambda r: (r.success, len(r.data)), reverse=True)
    return results


def _try_baud(port: str, baud: int, send_probe: bool) -> ProbeResult:
    t0 = time.monotonic()
    result = ProbeResult(port=port, baud=baud, success=False)
    try:
        with serial.Serial(
            port, baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=PROBE_TIMEOUT,
        ) as ser:
            ser.reset_input_buffer()
            if send_probe:
                # Send CR/LF to tickle the remote end
                ser.write(b"\r\n")
                ser.flush()
            data = ser.read(READ_CHUNK)
            result.latency_ms = (time.monotonic() - t0) * 1000
            if data:
                result.success    = True
                result.data       = data
                result.device_type = _fingerprint(data)
    except serial.SerialException:
        pass
    return result


def _fingerprint(data: bytes) -> str:
    """Match raw bytes against known device signatures."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    for device, patterns in DEVICE_SIGNATURES.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return device
    # At least check if it's printable ASCII
    printable = sum(0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D) for b in data)
    if printable / max(len(data), 1) > 0.75:
        return "Unknown (ASCII data)"
    return "Unknown (binary/garbage)"


# ─── Auto-Detection ───────────────────────────────────────────────────────────

def auto_detect(ports: list[PortInfo]) -> Optional[tuple[str, int]]:
    """
    Probe candidate ports and pick the best (port, baud) combination.
    Returns None if nothing usable is found.
    """
    if not ports:
        return None

    # Only probe the top-3 candidates to keep it fast
    candidates = ports[:3]
    best: Optional[tuple[str, int, str]] = None
    best_score = -1

    cinfo(f"Auto-probing {len(candidates)} candidate port(s) …\n")

    for port_info in candidates:
        results = probe_baud_rates(port_info.device)
        for r in results:
            if not r.success:
                continue
            score = len(r.data)
            if "Unknown" not in r.device_type:
                score += 500    # big bonus for a known device
            if score > best_score:
                best_score = score
                best = (r.port, r.baud, r.device_type)
                port_info.detected_baud   = r.baud
                port_info.detected_device = r.device_type

    if best:
        return (best[0], best[1])
    # Fall back: return top-scored port at 115200
    cprint("[yellow]No data received during probing. Defaulting to 115200 bps.[/yellow]")
    return (candidates[0].device, 115200)


# ─── Interactive Terminal ─────────────────────────────────────────────────────

class SerialTerminal:
    """Minimal interactive serial terminal with Ctrl-] to quit."""

    def __init__(self, port: str, baud: int):
        self.port    = port
        self.baud    = baud
        self.running = False
        self._ser: Optional[serial.Serial] = None

    def open(self) -> bool:
        try:
            self._ser = serial.Serial(
                self.port, self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
            return True
        except serial.SerialException as e:
            cerror(f"Cannot open {self.port}: {e}")
            return False

    def close(self) -> None:
        self.running = False
        if self._ser and self._ser.is_open:
            self._ser.close()

    def run(self) -> None:
        if not self.open():
            return

        self.running = True
        cprint(
            Panel(
                f"[bold green]Connected to [cyan]{self.port}[/cyan] @ "
                f"[cyan]{self.baud}[/cyan] bps[/bold green]\n"
                "[dim]Press  Ctrl-]  to disconnect[/dim]",
                box=box.ROUNDED,
            )
            if RICH_AVAILABLE else
            f"\n=== Connected to {self.port} @ {self.baud} bps ===\n"
            "Press Ctrl-] to disconnect\n"
        )

        rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        rx_thread.start()
        self._tx_loop()
        self.close()
        cprint("\n[bold yellow]Disconnected.[/bold yellow]")

    # ── RX ──────────────────────────────────────────────────────────────────

    def _rx_loop(self) -> None:
        while self.running:
            try:
                data = self._ser.read(READ_CHUNK)
                if data:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
            except Exception:
                break

    # ── TX ──────────────────────────────────────────────────────────────────

    def _tx_loop(self) -> None:
        """
        Read keyboard input and send to serial port.
        Ctrl-]  (ASCII 29) = disconnect.
        Works on both Unix (raw mode) and Windows (msvcrt).
        """
        if platform.system() == "Windows":
            self._tx_windows()
        else:
            self._tx_unix()

    def _tx_unix(self) -> None:
        import tty, termios
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while self.running:
                ch = sys.stdin.buffer.read(1)
                if not ch or ch == b"\x1d":   # Ctrl-]
                    break
                self._ser.write(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _tx_windows(self) -> None:
        import msvcrt
        while self.running:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b"\x1d":   # Ctrl-]
                    break
                self._ser.write(ch)
            else:
                time.sleep(0.01)


# ─── Report ───────────────────────────────────────────────────────────────────

def display_probe_results(results: list[ProbeResult]) -> None:
    if console and RICH_AVAILABLE:
        tbl = Table(
            title="[bold cyan]Baud Rate Probe Results[/bold cyan]",
            box=box.ROUNDED,
            show_lines=True,
        )
        tbl.add_column("Baud Rate",   style="cyan",  width=12, justify="right")
        tbl.add_column("Data Rcvd",   style="white", width=10, justify="right")
        tbl.add_column("Latency",     style="yellow",width=10, justify="right")
        tbl.add_column("Device Type", style="green", min_width=24)
        tbl.add_column("Status",      style="white", width=8)

        for r in sorted(results, key=lambda x: x.baud):
            status = "[bold green]✓ HIT[/bold green]" if r.success else "[dim]—[/dim]"
            tbl.add_row(
                f"{r.baud:,}",
                f"{len(r.data)} B" if r.data else "—",
                f"{r.latency_ms:.0f} ms" if r.success else "—",
                r.device_type if r.success else "—",
                status,
            )
        console.print()
        console.print(tbl)
    else:
        print(f"\n{'Baud':>10} {'Bytes':>7} {'Device Type'}")
        print("-" * 50)
        for r in sorted(results, key=lambda x: x.baud):
            if r.success:
                print(f"{r.baud:>10,} {len(r.data):>7}  {r.device_type}")
            else:
                print(f"{r.baud:>10,} {'—':>7}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Serial Console Auto-Detection Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--port",    "-p", help="Serial port (e.g. COM3, /dev/ttyUSB0)")
    p.add_argument("--baud",    "-b", type=int, default=115200, help="Baud rate (default: 115200)")
    p.add_argument("--auto",    "-a", action="store_true", help="Auto-detect port and baud rate")
    p.add_argument("--probe",         action="store_true", help="Probe baud rates on all/specified port(s)")
    p.add_argument("--connect", "-c", action="store_true", help="Connect after detection/selection")
    p.add_argument("--list",    "-l", action="store_true", help="List all ports and exit")
    p.add_argument("--no-rich",       action="store_true", help="Disable rich formatting")
    return p


def interactive_select(ports: list[PortInfo]) -> Optional[PortInfo]:
    """Let the user pick a port interactively."""
    if not ports:
        return None
    if console and RICH_AVAILABLE:
        choice = Prompt.ask(
            "\n[bold cyan]Select port number[/bold cyan] (or 'q' to quit)",
            default="1",
        )
    else:
        choice = input("\nSelect port number (or 'q' to quit) [1]: ") or "1"

    if choice.lower() == "q":
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(ports):
            return ports[idx]
    except ValueError:
        pass
    cerror("Invalid selection.")
    return None


# ─── Entry-point ─────────────────────────────────────────────────────────────

def main() -> None:
    global console, RICH_AVAILABLE

    parser = build_arg_parser()
    args   = parser.parse_args()

    if args.no_rich:
        RICH_AVAILABLE = False
        console        = None

    # ── Header ──────────────────────────────────────────────────────────────
    if console and RICH_AVAILABLE:
        console.print(Panel(
            "[bold cyan]Serial Console Auto-Detection Tool[/bold cyan]\n"
            "[dim]Detects, probes, and connects to serial consoles automatically[/dim]",
            box=box.DOUBLE_EDGE,
            style="cyan",
        ))
    else:
        print("=" * 55)
        print("  Serial Console Auto-Detection Tool")
        print("=" * 55)

    cinfo(f"Platform: {platform.system()} {platform.release()}")
    cinfo(f"Python:   {sys.version.split()[0]}")
    cinfo(f"pySerial: {serial.VERSION}\n")

    # ── Discover ports ──────────────────────────────────────────────────────
    cinfo("Scanning for serial ports …")
    ports = discover_ports()
    display_ports(ports)

    if args.list:
        return

    # ── --probe ─────────────────────────────────────────────────────────────
    if args.probe:
        target_port = args.port
        if not target_port:
            sel = interactive_select(ports)
            if not sel:
                return
            target_port = sel.device
        cinfo(f"\nProbing baud rates on {target_port} …")
        results = probe_baud_rates(target_port)
        display_probe_results(results)
        if any(r.success for r in results):
            best = next(r for r in results if r.success)
            csuccess(f"Best match: {best.baud} bps  →  {best.device_type}")
            if console and RICH_AVAILABLE:
                connect = Confirm.ask("\nOpen terminal session?", default=True)
            else:
                connect = input("\nOpen terminal session? [Y/n]: ").strip().lower() != "n"
            if connect:
                SerialTerminal(target_port, best.baud).run()
        return

    # ── --auto ───────────────────────────────────────────────────────────────
    if args.auto:
        result = auto_detect(ports)
        if result:
            port, baud = result
            csuccess(f"Auto-selected: {port} @ {baud} bps")
            SerialTerminal(port, baud).run()
        else:
            cerror("Auto-detection failed. Try --probe for manual baud probing.")
        return

    # ── --port / interactive ─────────────────────────────────────────────────
    target_port = args.port
    target_baud = args.baud

    if not target_port:
        if not ports:
            return
        sel = interactive_select(ports)
        if not sel:
            return
        target_port = sel.device
        if console and RICH_AVAILABLE:
            baud_str = Prompt.ask(
                "[bold cyan]Baud rate[/bold cyan]",
                default=str(target_baud),
            )
        else:
            baud_str = input(f"Baud rate [{target_baud}]: ").strip() or str(target_baud)
        try:
            target_baud = int(baud_str)
        except ValueError:
            cerror(f"Invalid baud rate '{baud_str}'. Using {target_baud}.")

    if args.connect or (not args.port):
        SerialTerminal(target_port, target_baud).run()
    else:
        csuccess(f"Ready to connect: {target_port} @ {target_baud} bps")
        cprint("  Run with [bold]--connect[/bold] to open a terminal session.")


if __name__ == "__main__":
    main()
