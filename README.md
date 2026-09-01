# UART Telnet Bridge

Python application that bridges a USB UART device to a Telnet TCP port.

## Behavior

- UART settings are fixed at `115200 baud`, `8 data bits`, `1 stop bit`, `no parity`
- Telnet authentication is bypassed; a client can connect directly
- Default Telnet TCP port is `23`
- `--port` can override the listen port
- `--greedy` discovers every available UART and assigns consecutive Telnet ports,
  beginning with `--port`
- Greedy bridges retain their port assignment if a UART disappears and retry that
  UART every 5 seconds until it is available again
- UARTs discovered after startup receive the next unused Telnet port
- One bridge-lifetime thread continuously reads UART data and forwards it to the
  Telnet client when one is connected
- One thread forwards Telnet data to the UART
- If no Telnet client is connected, UART receive data is continuously drained and
  dropped so stale data cannot accumulate in the device or OS receive buffers
- One active Telnet client is served at a time
- If a new Telnet client connects, the existing client connection is terminated and replaced by the new one

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Usage

```bash
python3 uart_telnet_bridge.py /dev/ttyUSB0
```

Use a different TCP port:

```bash
python3 uart_telnet_bridge.py /dev/ttyUSB0 --port 2323
```

On Unix-like systems, binding to TCP port `23` may require elevated privileges.

On Windows, use a COM port name such as:

```powershell
python uart_telnet_bridge.py COM3 --port 2323
```

Bridge every available UART, beginning with TCP port 2323:

```bash
python3 uart_telnet_bridge.py --greedy --port 2323
```

For example, if the discovered UARTs are `/dev/ttyUSB0`, `/dev/ttyUSB1`, and
`/dev/ttyUSB2`, they listen on ports `2323`, `2324`, and `2325`, respectively.
If one is unplugged, its Telnet listener remains assigned and the UART is reopened
when it becomes available again.
