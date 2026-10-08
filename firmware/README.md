# Front panel firmware

The panel is optional. With nothing attached the app logs one line saying so and
the web UI takes control of all three switches.

## Protocol

The board sends one line per reading over USB serial at **9600 baud**:

```
Switch 1: ON  | Switch 2: OFF | Switch 3: OFF
```

- Three readings per line, separated by `|`, in switch order.
- Each is `Switch <n>: ON` or `Switch <n>: OFF`.
- Parsing is case-insensitive and ignores spacing, so `switch 1:on|...` is
  accepted. Any line that does not contain all three switches is ignored, which
  makes boot banners and noise harmless.
- Lines may be sent continuously; the app applies only what changed.

| Switch | Controls |
| --- | --- |
| 1 | Declicker |
| 2 | Denoiser |
| 3 | Monitor, output only what is being removed |

The panel owns these three settings: while it is connected the API refuses to
change them, so the physical switches and the page can never disagree. If the
board is unplugged the API accepts them again as a fallback.

## Sketch

`vinyl_panel/vinyl_panel.ino` is a reference implementation for an Arduino with
three toggle switches wired to pins 2, 3 and 4, each switched to ground using
the internal pull-ups. It was written from the protocol above rather than
recovered from the original board, so treat it as a starting point: any device
that emits the same lines will work.

Port and baud rate are configurable from the app side, see `serial_port` and
`serial_baud` in `settings.json`, or `VINYL_SERIAL_PORT` and `VINYL_SERIAL_BAUD`.
