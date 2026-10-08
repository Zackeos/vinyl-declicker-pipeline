"""
Front panel listener.

Reads lines like "Switch 1: ON | Switch 2: OFF | Switch 3: OFF" over serial.
Switch 1 is the declicker, 2 the denoiser, 3 the monitor. Optional: with no
panel attached the web UI controls them instead.
"""
import re
import threading
import time

import serial

DEFAULT_SERIAL_PORT = '/dev/ttyACM0'
DEFAULT_SERIAL_BAUD = 9600
KEYS = ('declicker_on', 'denoiser_on', 'monitor')
LINE_RE = re.compile(r'Switch\s*(\d)\s*:\s*(ON|OFF)', re.IGNORECASE)


def parse_switch_state(line):
    """Returns (sw1, sw2, sw3) booleans, or None if the line isn't a full reading."""
    found = {int(n): state.upper() == 'ON' for n, state in LINE_RE.findall(line)}
    if not all(n in found for n in (1, 2, 3)):
        return None
    return found[1], found[2], found[3]


def serial_listener(engine):
    panel = engine.panel
    ser = None
    last = None
    announced_missing = False
    while True:
        port = engine.opts.get('serial_port', DEFAULT_SERIAL_PORT)
        baud = int(engine.opts.get('serial_baud', DEFAULT_SERIAL_BAUD))
        try:
            if ser is None:
                ser = serial.Serial(port, baud, timeout=1)
                announced_missing = False
                panel['connected'] = True
                last = None
                print("Panel: connected to Arduino", flush=True)

            line = ser.readline().decode('utf-8', errors='replace').strip()
            if not line:
                continue
            panel['raw'] = line
            states = parse_switch_state(line)
            if states is None:
                continue

            panel['switches'] = dict(zip(KEYS, states))
            changes = {k: s for k, s, prev in zip(KEYS, states, last or (None,) * 3) if s != prev}
            last = states
            if changes:
                print("Panel: " + ", ".join(f"{k}={'ON' if v else 'OFF'}" for k, v in changes.items()), flush=True)
                engine.set_options(changes, from_panel=True)

        except (serial.SerialException, OSError):
            if ser is not None:
                print("Panel: lost connection to Arduino, retrying", flush=True)
                try:
                    ser.close()
                except Exception:
                    pass
            ser = None
            panel['connected'] = False
            panel['switches'] = None
            if not announced_missing:
                # Not an error: the web UI controls the switches instead.
                print(f"Panel: none found on {port}; web UI has control", flush=True)
                announced_missing = True
            time.sleep(5)
        except Exception as e:
            print(f"Panel: unexpected error: {e}", flush=True)
            time.sleep(2)


def start_listener(engine):
    threading.Thread(target=serial_listener, args=(engine,), daemon=True).start()
