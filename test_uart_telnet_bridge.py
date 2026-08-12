import queue
import threading
import unittest

from uart_telnet_bridge import UartTelnetBridge


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


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.send_event = threading.Event()

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self.send_event.set()


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


if __name__ == "__main__":
    unittest.main()
