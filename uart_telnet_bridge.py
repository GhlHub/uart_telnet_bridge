#!/usr/bin/env python3
"""Bridge a USB UART device to a Telnet TCP socket."""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time

import serial


DEFAULT_TELNET_PORT = 23
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


def strip_telnet_commands(data: bytes) -> bytes:
    """Remove Telnet negotiation commands from payload bytes."""
    output = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte != IAC:
            output.append(byte)
            i += 1
            continue

        if i + 1 >= len(data):
            break

        command = data[i + 1]
        if command == IAC:
            output.append(IAC)
            i += 2
            continue

        if command in (DO, DONT, WILL, WONT):
            i += 3
            continue

        if command == SB:
            i += 2
            while i + 1 < len(data):
                if data[i] == IAC and data[i + 1] == SE:
                    i += 2
                    break
                i += 1
            continue

        i += 2

    return bytes(output)


class UartTelnetBridge:
    def __init__(self, serial_port: str, telnet_port: int) -> None:
        self.serial_port_name = serial_port
        self.telnet_port = telnet_port
        self.shutdown_event = threading.Event()
        self.client_lock = threading.Lock()
        self.active_client_socket: socket.socket | None = None
        self.active_client_addr: tuple[str, int] | None = None
        self.active_client_stop_event: threading.Event | None = None
        self.active_client_ready_event: threading.Event | None = None
        self.active_client_thread: threading.Thread | None = None
        self.uart_reader_thread: threading.Thread | None = None
        self.server_socket: socket.socket | None = None
        self.serial_handle = serial.Serial(
            port=self.serial_port_name,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=0.2,
        )

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
                    "Telnet client %s:%s disconnected in favor of %s:%s",
                    previous_addr[0],
                    previous_addr[1],
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
        logging.info("Telnet client connected from %s:%s", client_addr[0], client_addr[1])
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
                "Telnet client disconnected from %s:%s", client_addr[0], client_addr[1]
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
            try:
                data = self.serial_handle.read(BUFFER_SIZE)
            except serial.SerialException as exc:
                logging.error("UART read failed: %s", exc)
                self.shutdown_event.set()
                return

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

            uart_payload = strip_telnet_commands(data)
            if not uart_payload:
                continue

            try:
                self.serial_handle.write(uart_payload)
                self.serial_handle.flush()
            except serial.SerialException as exc:
                logging.error("UART write failed: %s", exc)
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
        if self.serial_handle.is_open:
            self.serial_handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge a USB UART device to a Telnet TCP socket."
    )
    parser.add_argument(
        "serial_port",
        help="UART device path such as /dev/ttyUSB0 or COM3",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )

    bridge = UartTelnetBridge(serial_port=args.serial_port, telnet_port=args.port)
    try:
        bridge.run()
    except KeyboardInterrupt:
        logging.info("Shutting down")
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
