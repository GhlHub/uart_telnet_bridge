import queue
import threading
import time
import unittest
from dataclasses import dataclass

import serial

from uart_telnet_bridge import (
    GreedyUartTelnetBridge, UartTelnetBridge, TelnetInputDecoder, parse_args,
)


class TelnetInputTests(unittest.TestCase):
    def test_enter_encodings_and_fragmentation(self) -> None:
        stream = b"echo one\r\0echo two\r\necho three\rnext\n"
        expected = b"echo one\recho two\recho three\rnext\n"
        for split in range(len(stream) + 1):
            with self.subTest(split=split):
                decoder = TelnetInputDecoder()
                self.assertEqual(decoder.feed(stream[:split]) +
                                 decoder.feed(stream[split:]), expected)

    def test_commands_do_not_break_cr_padding_state(self) -> None:
        stream = b"one\r\xff\xfd\x03\0two\r\xff\xfa\x18payload\xff\xffx\xff\xf0\nend"
        decoder = TelnetInputDecoder()
        self.assertEqual(b"".join(decoder.feed(bytes([b])) for b in stream),
                         b"one\rtwo\rend")

    def test_plain_cr_is_immediate_and_literal_iac_survives(self) -> None:
        decoder = TelnetInputDecoder()
        self.assertEqual(decoder.feed(b"one\r"), b"one\r")
        self.assertEqual(decoder.feed(b"two\xff"), b"two")
        self.assertEqual(decoder.feed(b"\xff\0"), b"\xff\0")
        self.assertEqual(decoder.feed(b"\r\r\0"), b"\r\r")

    def test_connection_state_is_not_shared(self) -> None:
        first = TelnetInputDecoder()
        first.feed(b"\xff\xfb")
        self.assertEqual(TelnetInputDecoder().feed(b"echo ok\r\0"), b"echo ok\r")

    def test_uart_loop_retains_decoder_between_receives(self) -> None:
        class Client:
            chunks = iter([b"one\r", b"\0two\r\xff", b"\xfd", b"\x03\n", b""])
            def recv(self, _size):
                return next(self.chunks)

        class Uart:
            def __init__(self):
                self.output = bytearray()
            def write(self, data):
                self.output.extend(data)
            def flush(self):
                pass

        bridge = object.__new__(UartTelnetBridge)
        bridge.shutdown_event = threading.Event()
        bridge.serial_handle = Uart()
        stop = threading.Event()
        bridge.telnet_to_uart_loop(Client(), stop)
        self.assertEqual(bridge.serial_handle.output, b"one\rtwo\r")
        self.assertTrue(stop.is_set())


class FakeSerial:
    def __init__(self) -> None:
        self.input_queue: queue.Queue[tuple[bytes, threading.Event]] = queue.Queue()
        self.read_condition = threading.Condition()
        self.read_count = 0

    def feed(self, data: bytes) -> threading.Event:
        read_event = threading.Event()
        self.input_queue.put((data, read_event))
        return read_event

    def read(self, _size: int) -> bytes:
        with self.read_condition:
            self.read_count += 1
            self.read_condition.notify_all()
        try:
            data, read_event = self.input_queue.get(timeout=0.01)
        except queue.Empty:
            return b""
        read_event.set()
        return data

    def wait_for_read_after(self, previous_count: int) -> bool:
        with self.read_condition:
            return self.read_condition.wait_for(
                lambda: self.read_count > previous_count, timeout=1
            )


class DisconnectingSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.closed = threading.Event()

    def read(self, _size: int) -> bytes:
        raise serial.SerialException("device disconnected")

    def close(self) -> None:
        self.is_open = False
        self.closed.set()


class ReattachedSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.read_event = threading.Event()

    def read(self, _size: int) -> bytes:
        self.read_event.set()
        time.sleep(0.005)
        return b""

    def close(self) -> None:
        self.is_open = False


class ReconnectTestBridge(UartTelnetBridge):
    def __init__(self) -> None:
        self.serial_handles = [DisconnectingSerial(), ReattachedSerial()]
        self.open_count = 0
        super().__init__(
            serial_port="COM3",
            telnet_port=2300,
            auto_reconnect=True,
            reconnect_interval=0.01,
        )

    def open_serial(self) -> DisconnectingSerial | ReattachedSerial:
        serial_handle = self.serial_handles[self.open_count]
        self.open_count += 1
        return serial_handle


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.send_event = threading.Event()

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self.send_event.set()


class FakeClientSocket(FakeSocket):
    def settimeout(self, _timeout: float) -> None:
        pass

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass
class FakePortInfo:
    device: str


class FakeBridge:
    def __init__(
        self,
        serial_port: str,
        telnet_port: int,
        *,
        auto_reconnect: bool,
        reconnect_interval: float,
    ) -> None:
        self.serial_port_name = serial_port
        self.telnet_port = telnet_port
        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self) -> None:
        self.started.set()
        self.stopped.wait(timeout=1)

    def close(self) -> None:
        self.stopped.set()


class UartReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = object.__new__(UartTelnetBridge)
        self.bridge.shutdown_event = threading.Event()
        self.bridge.client_lock = threading.Lock()
        self.bridge.active_client_socket = None
        self.bridge.active_client_stop_event = None
        self.bridge.active_client_ready_event = None
        self.bridge.serial_handle = FakeSerial()
        self.reader_thread = threading.Thread(target=self.bridge.uart_reader_loop)
        self.reader_thread.start()

    def tearDown(self) -> None:
        self.bridge.shutdown_event.set()
        self.reader_thread.join(timeout=1)
        self.assertFalse(self.reader_thread.is_alive())

    def test_discards_uart_data_until_client_is_ready(self) -> None:
        client_socket = FakeSocket()
        stop_event = threading.Event()
        ready_event = threading.Event()

        with self.bridge.client_lock:
            self.bridge.active_client_socket = client_socket
            self.bridge.active_client_stop_event = stop_event
            self.bridge.active_client_ready_event = ready_event

        discarded_read = self.bridge.serial_handle.feed(b"discarded")
        self.assertTrue(discarded_read.wait(timeout=1))
        read_count = self.bridge.serial_handle.read_count
        self.assertTrue(self.bridge.serial_handle.wait_for_read_after(read_count))
        self.assertEqual(client_socket.sent, [])

        ready_event.set()
        forwarded_read = self.bridge.serial_handle.feed(b"forwarded")
        self.assertTrue(forwarded_read.wait(timeout=1))
        self.assertTrue(client_socket.send_event.wait(timeout=1))
        self.assertEqual(client_socket.sent, [b"forwarded"])


class ClientLoggingTests(unittest.TestCase):
    def test_connect_and_disconnect_logs_include_uart_name(self) -> None:
        bridge = object.__new__(UartTelnetBridge)
        bridge.serial_port_name = "COM3"
        bridge.shutdown_event = threading.Event()
        bridge.shutdown_event.set()
        bridge.client_lock = threading.Lock()
        bridge.active_client_socket = None
        bridge.active_client_addr = None
        bridge.active_client_stop_event = None
        bridge.active_client_ready_event = None
        bridge.active_client_thread = None

        with self.assertLogs(level="INFO") as captured_logs:
            bridge.handle_client(
                FakeClientSocket(),
                ("10.0.1.24", 60972),
                threading.Event(),
                threading.Event(),
            )

        self.assertIn(
            "Telnet client connected from 10.0.1.24:60972 to UART COM3",
            captured_logs.output[0],
        )
        self.assertIn(
            "Telnet client disconnected from 10.0.1.24:60972 and UART COM3",
            captured_logs.output[1],
        )


class GreedyBridgeTests(unittest.TestCase):
    def test_assigns_stable_sequential_ports_and_adds_new_uarts(self) -> None:
        available_devices = [FakePortInfo("COM10"), FakePortInfo("COM3")]
        greedy_bridge = GreedyUartTelnetBridge(
            first_telnet_port=2300,
            poll_interval=0.01,
            port_enumerator=lambda: available_devices,
            bridge_factory=FakeBridge,
        )
        self.addCleanup(greedy_bridge.close)

        greedy_bridge.discover_uarts()
        self.assertEqual(
            {
                device: bridge.telnet_port
                for device, bridge in greedy_bridge.bridges.items()
            },
            {"COM3": 2300, "COM10": 2301},
        )
        self.assertTrue(
            all(bridge.auto_reconnect for bridge in greedy_bridge.bridges.values())
        )

        original_com10_bridge = greedy_bridge.bridges["COM10"]
        available_devices[:] = [FakePortInfo("COM3")]
        greedy_bridge.discover_uarts()
        self.assertNotIn("COM10", greedy_bridge.bridges)
        self.assertTrue(original_com10_bridge.stopped.is_set())
        self.assertEqual(greedy_bridge.port_assignments["COM10"], 2301)

        available_devices[:] = [FakePortInfo("COM10"), FakePortInfo("COM3")]
        greedy_bridge.discover_uarts()
        self.assertEqual(greedy_bridge.bridges["COM10"].telnet_port, 2301)
        self.assertIsNot(greedy_bridge.bridges["COM10"], original_com10_bridge)

        available_devices.append(FakePortInfo("COM7"))
        greedy_bridge.discover_uarts()
        self.assertEqual(greedy_bridge.bridges["COM7"].telnet_port, 2302)
        self.assertEqual(
            greedy_bridge.port_assignments,
            {"COM3": 2300, "COM10": 2301, "COM7": 2302},
        )

    def test_reattaches_a_disconnected_uart(self) -> None:
        bridge = ReconnectTestBridge()
        reader_thread = threading.Thread(target=bridge.uart_reader_loop)
        bridge.uart_reader_thread = reader_thread
        reader_thread.start()
        self.addCleanup(bridge.close)

        disconnected_serial, reattached_serial = bridge.serial_handles
        self.assertTrue(disconnected_serial.closed.wait(timeout=1))
        self.assertTrue(reattached_serial.read_event.wait(timeout=1))
        self.assertEqual(bridge.open_count, 2)

    def test_greedy_cli_does_not_require_a_serial_port(self) -> None:
        args = parse_args(["--greedy", "--port", "2300"])
        self.assertTrue(args.greedy)
        self.assertIsNone(args.serial_port)
        self.assertEqual(args.port, 2300)


if __name__ == "__main__":
    unittest.main()
