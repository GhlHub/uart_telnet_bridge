#!/usr/bin/env python3
"""Bridge a USB UART device to a Telnet TCP socket."""

from __future__ import annotations

import argparse
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable

import serial
from serial.tools import list_ports


DEFAULT_TELNET_PORT = 23
DEFAULT_RECONNECT_INTERVAL = 5.0
BUFFER_SIZE = 4096

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240
ECHO = 1
SUPPRESS_GO_AHEAD = 3


class TelnetInputDecoder:
    """Decode an NVT console stream, retaining state across TCP recv calls.

    Emit CR immediately for interactive input. Suppress the following NVT NUL
    or LF so one Enter produces one UART carriage return. Negotiation and
    subnegotiation bytes are not UART data, even when fragmented by TCP.
    This bridge does not negotiate Telnet BINARY mode.
    """

    def __init__(self) -> None:
        self.state = "data"
        self.after_cr = False

    def feed(self, data: bytes) -> bytes:
        output = bytearray()
        for byte in data:
            if self.state == "option":
                self.state = "data"
                continue
            if self.state == "subneg":
                if byte == IAC:
                    self.state = "subneg_iac"
                continue
            if self.state == "subneg_iac":
                self.state = "data" if byte == SE else "subneg"
                continue
            if self.state == "iac":
                if byte in (DO, DONT, WILL, WONT):
                    self.state = "option"
                    continue
                if byte == SB:
                    self.state = "subneg"
                    continue
                self.state = "data"
                if byte != IAC:
                    continue
            elif byte == IAC:
                self.state = "iac"
                continue

            if self.after_cr:
                self.after_cr = False
                if byte in (0, 10):
                    continue
            output.append(byte)
            self.after_cr = byte == 13
        return bytes(output)


def strip_telnet_commands(data: bytes) -> bytes:
    """Decode a complete NVT chunk; stream users must retain a decoder."""
    return TelnetInputDecoder().feed(data)


def natural_port_sort_key(device: str) -> list[object]:
    """Sort COM/tty names numerically (for example, COM3 before COM10)."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", device)
    ]


class UartTelnetBridge:
    def __init__(
        self,
        serial_port: str,
        telnet_port: int,
        *,
        auto_reconnect: bool = False,
        reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL,
    ) -> None:
        self.serial_port_name = serial_port
        self.telnet_port = telnet_port
        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval
        self.reconnect_not_before = 0.0
        self.shutdown_event = threading.Event()
        self.serial_lock = threading.Lock()
        self.client_lock = threading.Lock()
        self.active_client_socket: socket.socket | None = None
        self.active_client_addr: tuple[str, int] | None = None
        self.active_client_stop_event: threading.Event | None = None
        self.active_client_ready_event: threading.Event | None = None
        self.active_client_thread: threading.Thread | None = None
        self.uart_reader_thread: threading.Thread | None = None
        self.server_socket: socket.socket | None = None
        self.serial_handle: serial.Serial | None = None
        try:
            self.serial_handle = self.open_serial()
        except serial.SerialException:
            if not self.auto_reconnect:
                raise
            self.reconnect_not_before = time.monotonic() + self.reconnect_interval
            logging.warning(
                "UART %s is unavailable; retrying every %.1f seconds",
                self.serial_port_name,
                self.reconnect_interval,
            )

    def open_serial(self) -> serial.Serial:
        return serial.Serial(
            port=self.serial_port_name,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=0.2,
        )

    def reconnect_uart(self) -> serial.Serial | None:
        """Open a detached UART, returning None when it is still unavailable."""
        with self.serial_lock:
            if self.serial_handle is not None:
                return self.serial_handle
            if time.monotonic() < self.reconnect_not_before:
                return None
            try:
                serial_handle = self.open_serial()
            except serial.SerialException as exc:
                self.reconnect_not_before = (
                    time.monotonic() + self.reconnect_interval
                )
                logging.debug(
                    "UART %s is still unavailable: %s", self.serial_port_name, exc
                )
                return None
            self.serial_handle = serial_handle

        logging.info("Attached to UART %s", self.serial_port_name)
        return serial_handle

    def detach_uart(self, serial_handle: serial.Serial) -> None:
        """Close serial_handle if it is still the bridge's current UART handle."""
        with self.serial_lock:
            if self.serial_handle is not serial_handle:
                return
            self.serial_handle = None
            self.reconnect_not_before = time.monotonic() + self.reconnect_interval
        try:
            serial_handle.close()
        except (OSError, serial.SerialException):
            pass

    def stop_active_client(self) -> None:
        with self.client_lock:
            stop_event = self.active_client_stop_event
        if stop_event is not None:
            stop_event.set()

    def run(self) -> None:
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("0.0.0.0", self.telnet_port))
        self.server_socket.listen(1)
        self.server_socket.settimeout(1.0)

        logging.info(
            "Listening on 0.0.0.0:%s and forwarding to UART %s",
            self.telnet_port,
            self.serial_port_name,
        )

        self.uart_reader_thread = threading.Thread(
            target=self.uart_reader_loop,
            name="uart-reader",
            daemon=True,
        )
        self.uart_reader_thread.start()

        try:
            while not self.shutdown_event.is_set():
                try:
                    client_socket, client_addr = self.server_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.shutdown_event.is_set():
                        break
                    raise

                self.replace_client(client_socket, client_addr)
        finally:
            self.close()

    def replace_client(
        self, client_socket: socket.socket, client_addr: tuple[str, int]
    ) -> None:
        with self.client_lock:
            previous_socket = self.active_client_socket
            previous_addr = self.active_client_addr
            previous_stop_event = self.active_client_stop_event
            previous_ready_event = self.active_client_ready_event

            if previous_stop_event is not None:
                previous_stop_event.set()
            if previous_ready_event is not None:
                previous_ready_event.clear()
            if previous_socket is not None:
                try:
                    previous_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                previous_socket.close()

            if previous_addr is not None:
                logging.info(
                    "Telnet client %s:%s disconnected from UART %s in favor of %s:%s",
                    previous_addr[0],
                    previous_addr[1],
                    self.serial_port_name,
                    client_addr[0],
                    client_addr[1],
                )

            stop_event = threading.Event()
            ready_event = threading.Event()
            session_thread = threading.Thread(
                target=self.handle_client,
                name=f"client-session-{client_addr[0]}:{client_addr[1]}",
                args=(client_socket, client_addr, stop_event, ready_event),
                daemon=True,
            )
            self.active_client_socket = client_socket
            self.active_client_addr = client_addr
            self.active_client_stop_event = stop_event
            self.active_client_ready_event = ready_event
            self.active_client_thread = session_thread
            session_thread.start()

    def handle_client(
        self,
        client_socket: socket.socket,
        client_addr: tuple[str, int],
        stop_event: threading.Event,
        ready_event: threading.Event,
    ) -> None:
        logging.info(
            "Telnet client connected from %s:%s to UART %s",
            client_addr[0],
            client_addr[1],
            self.serial_port_name,
        )
        client_socket.settimeout(0.5)
        self.send_telnet_banner(client_socket)
        telnet_to_uart = threading.Thread(
            target=self.telnet_to_uart_loop,
            name="telnet-to-uart",
            args=(client_socket, stop_event),
            daemon=True,
        )

        telnet_to_uart.start()
        ready_event.set()

        try:
            while not self.shutdown_event.is_set() and not stop_event.is_set():
                time.sleep(0.1)
        finally:
            ready_event.clear()
            stop_event.set()
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client_socket.close()
            telnet_to_uart.join()
            logging.info(
                "Telnet client disconnected from %s:%s and UART %s",
                client_addr[0],
                client_addr[1],
                self.serial_port_name,
            )
            with self.client_lock:
                if self.active_client_socket is client_socket:
                    self.active_client_socket = None
                    self.active_client_addr = None
                    self.active_client_stop_event = None
                    self.active_client_ready_event = None
                    self.active_client_thread = None

    def send_telnet_banner(self, client_socket: socket.socket) -> None:
        negotiation = bytes(
            [
                IAC,
                WILL,
                SUPPRESS_GO_AHEAD,
                IAC,
                WILL,
                ECHO,
            ]
        )
        try:
            client_socket.sendall(negotiation)
        except OSError:
            pass

    def uart_reader_loop(self) -> None:
        """Continuously drain UART input and forward it to the active client."""
        while not self.shutdown_event.is_set():
            serial_handle = self.serial_handle
            if serial_handle is None:
                if not self.auto_reconnect:
                    self.shutdown_event.set()
                    return
                reconnect_delay = max(
                    0.0, self.reconnect_not_before - time.monotonic()
                )
                if self.shutdown_event.wait(reconnect_delay):
                    return
                serial_handle = self.reconnect_uart()
                if serial_handle is None:
                    continue

            try:
                data = serial_handle.read(BUFFER_SIZE)
            except serial.SerialException as exc:
                logging.error("UART %s read failed: %s", self.serial_port_name, exc)
                if not self.auto_reconnect:
                    self.shutdown_event.set()
                    return
                self.detach_uart(serial_handle)
                self.stop_active_client()
                logging.warning(
                    "UART %s detached; retrying every %.1f seconds",
                    self.serial_port_name,
                    self.reconnect_interval,
                )
                continue

            if not data:
                continue

            with self.client_lock:
                client_socket = self.active_client_socket
                stop_event = self.active_client_stop_event
                ready_event = self.active_client_ready_event

            if (
                client_socket is None
                or stop_event is None
                or ready_event is None
                or not ready_event.is_set()
            ):
                continue

            try:
                client_socket.sendall(data)
            except OSError:
                stop_event.set()

    def telnet_to_uart_loop(
        self, client_socket: socket.socket, stop_event: threading.Event
    ) -> None:
        decoder = TelnetInputDecoder()
        while not self.shutdown_event.is_set() and not stop_event.is_set():
            try:
                data = client_socket.recv(BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                stop_event.set()
                return

            if not data:
                stop_event.set()
                return

            uart_payload = decoder.feed(data)
            if not uart_payload:
                continue

            try:
                serial_handle = self.serial_handle
                if serial_handle is None:
                    logging.warning("UART %s is unavailable", self.serial_port_name)
                    stop_event.set()
                    return
                serial_handle.write(uart_payload)
                serial_handle.flush()
            except serial.SerialException as exc:
                logging.error("UART %s write failed: %s", self.serial_port_name, exc)
                if self.auto_reconnect:
                    self.detach_uart(serial_handle)
                stop_event.set()
                return

    def close(self) -> None:
        self.shutdown_event.set()
        with self.client_lock:
            active_socket = self.active_client_socket
            active_stop_event = self.active_client_stop_event
            active_ready_event = self.active_client_ready_event
            active_thread = self.active_client_thread
            self.active_client_socket = None
            self.active_client_addr = None
            self.active_client_stop_event = None
            self.active_client_ready_event = None
            self.active_client_thread = None

        if active_ready_event is not None:
            active_ready_event.clear()
        if active_stop_event is not None:
            active_stop_event.set()
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active_socket.close()
        if active_thread is not None and active_thread.is_alive():
            active_thread.join()
        uart_reader_thread = self.uart_reader_thread
        if uart_reader_thread is not None and uart_reader_thread.is_alive():
            uart_reader_thread.join()
        self.uart_reader_thread = None
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None
        with self.serial_lock:
            serial_handle = self.serial_handle
            self.serial_handle = None
        if serial_handle is not None and serial_handle.is_open:
            serial_handle.close()


class GreedyUartTelnetBridge:
    """Run one Telnet bridge for every UART that is currently available."""

    def __init__(
        self,
        first_telnet_port: int,
        *,
        poll_interval: float = DEFAULT_RECONNECT_INTERVAL,
        port_enumerator: Callable[[], Iterable[object]] = list_ports.comports,
        bridge_factory: Callable[..., UartTelnetBridge] = UartTelnetBridge,
    ) -> None:
        self.next_telnet_port = first_telnet_port
        self.poll_interval = poll_interval
        self.port_enumerator = port_enumerator
        self.bridge_factory = bridge_factory
        self.shutdown_event = threading.Event()
        self.port_assignments: dict[str, int] = {}
        self.bridges: dict[str, UartTelnetBridge] = {}
        self.bridge_threads: dict[str, threading.Thread] = {}

    def discover_uarts(self) -> None:
        devices = sorted(
            {port.device for port in self.port_enumerator()}, key=natural_port_sort_key
        )
        available_devices = set(devices)

        for device in list(self.bridges):
            if device in available_devices:
                continue
            bridge = self.bridges.pop(device)
            thread = self.bridge_threads.pop(device)
            logging.info(
                "UART %s is unavailable; taking down Telnet port %s",
                device,
                bridge.telnet_port,
            )
            bridge.close()
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join()

        for device in devices:
            if device in self.bridges:
                continue
            telnet_port = self.port_assignments.get(device)
            if telnet_port is None:
                if self.next_telnet_port > 65535:
                    logging.error(
                        "Cannot assign a TCP port to UART %s: port range exhausted",
                        device,
                    )
                    continue
                telnet_port = self.next_telnet_port
                self.next_telnet_port += 1
                self.port_assignments[device] = telnet_port

            bridge = self.bridge_factory(
                serial_port=device,
                telnet_port=telnet_port,
                auto_reconnect=True,
                reconnect_interval=self.poll_interval,
            )
            thread = threading.Thread(
                target=bridge.run,
                name=f"bridge-{device}-{telnet_port}",
                daemon=True,
            )
            self.bridges[device] = bridge
            self.bridge_threads[device] = thread
            logging.info("Attached UART %s to Telnet port %s", device, telnet_port)
            thread.start()

    def run(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                self.discover_uarts()
                self.shutdown_event.wait(self.poll_interval)
        finally:
            self.close()

    def close(self) -> None:
        self.shutdown_event.set()
        for bridge in self.bridges.values():
            bridge.close()
        for thread in self.bridge_threads.values():
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge a USB UART device to a Telnet TCP socket."
    )
    parser.add_argument(
        "serial_port",
        nargs="?",
        help="UART device path such as /dev/ttyUSB0 or COM3",
    )
    parser.add_argument(
        "--greedy",
        action="store_true",
        help="bridge every discovered UART, assigning sequential Telnet ports",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_TELNET_PORT,
        help=f"Telnet listen port (default: {DEFAULT_TELNET_PORT})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    args = parser.parse_args(argv)
    if args.greedy and args.serial_port is not None:
        parser.error("serial_port must be omitted when --greedy is used")
    if not args.greedy and args.serial_port is None:
        parser.error("serial_port is required unless --greedy is used")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )

    if args.greedy:
        bridge = GreedyUartTelnetBridge(first_telnet_port=args.port)
    else:
        bridge = UartTelnetBridge(serial_port=args.serial_port, telnet_port=args.port)
    try:
        bridge.run()
    except KeyboardInterrupt:
        logging.info("Shutting down")
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
