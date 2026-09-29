# UART Telnet Bridge

Python application that bridges a USB UART device to a Telnet TCP port.

## Behavior

- UART settings are fixed at `115200 baud`, `8 data bits`, `1 stop bit`, `no parity`
- Telnet authentication is bypassed; a client can connect directly
- Default Telnet TCP port is `23`
- `--port` can override the listen port
- `--greedy` discovers every available UART and assigns consecutive Telnet ports,
  beginning with `--port`
- Greedy mode takes down a UART's Telnet listener when the UART disappears, while
  retaining its port assignment; if the UART returns, its listener is recreated
  on the same Telnet port
- UARTs discovered after startup receive the next unused Telnet port
- One bridge-lifetime thread continuously reads UART data and forwards it to the
  Telnet client when one is connected
- One thread forwards Telnet data to the UART
- Telnet input is decoded as a stream across TCP packet boundaries. `CR NUL`
  and `CR LF` both produce one UART carriage return, without a trailing NUL
  corrupting the next shell command. Telnet BINARY mode is not negotiated.
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
If one is unplugged, its Telnet listener is taken down. When the same UART becomes
available again, the listener is recreated on its previous Telnet port.

## Console carriage-return fix

Older versions forwarded the NUL after a Telnet carriage return to the UART.
On the KR260 BusyBox shell this displayed `~ # ?` and caused the following
command to fail as `?command: not found`. Input decoding now retains state
between socket reads, removes Telnet negotiation, and converts both `CR NUL`
and `CR LF` into a single UART CR.

Restart the bridge process after updating the script, then reconnect. No Linux
or FPGA rebuild, or client `toggle crlf` workaround, is needed. The user confirmed
correct operation on the KR260 Linux console after restarting the Windows-hosted
bridge.

Run the regression suite with:

```sh
python -m unittest -v
```

All ten tests passed, including split carriage-return sequences, fragmented
Telnet negotiations, escaped IAC bytes, per-connection state, and UART forwarding.
