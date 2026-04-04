# UART Telnet Bridge

Python application that bridges a USB UART device to a Telnet TCP port.

## Behavior

- UART settings are fixed at `115200 baud`, `8 data bits`, `1 stop bit`, `no parity`
- Telnet authentication is bypassed; a client can connect directly
- Default Telnet TCP port is `23`
- `--port` can override the listen port
- One thread forwards UART data to the Telnet client
- One thread forwards Telnet data to the UART
- If no Telnet client is connected, UART receive data is dropped
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
