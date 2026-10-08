"""Persistent settings stored as JSON next to the app (git-ignored)."""
import json
import os
import threading

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

DEFAULTS = {
    # --- audio device / stream -------------------------------------------------
    'input_device': None,    # device name; None = auto-detect
    'output_device': None,
    'sample_rate': 48000,    # PipeWire runs at 48 kHz, so this avoids resampling
    'buffer_size': 1024,

    # --- front panel (optional) ------------------------------------------------
    'serial_port': '/dev/ttyACM0',
    'serial_baud': 9600,
    # Tried in order when no device is saved, then anything usable.
    'preferred_devices': ['pipewire', 'default'],
    # Playback delay while filtering, covering ffmpeg lag.
    'latency_ms': 200,
    # Delay when bypassed. Moves between the two by resampling, not skipping.
    'idle_latency_ms': 15,
    'engage_rate_pct': 2.0,   # slow down this much to build the buffer (higher = filters sooner)
    'release_rate_pct': 0.5,  # speed up this much to shed it (lower = less audible)
    'idle_ffmpeg': True,      # stop feeding ffmpeg entirely when nothing is engaged

    # --- switch positions (owned by the Arduino front panel) -------------------
    'declicker_on': True,
    'denoiser_on': False,

    # --- declicker -------------------------------------------------------------
    'sensitivity': 5.0,      # 0..10, gentle to aggressive
    'auto_sensitivity': True,   # tune sensitivity from how much is being removed
    # Cost depends on the audio: on a near-silent input adeclick finds impulses
    # everywhere. The overload guard turns these off again if ffmpeg lags.
    'two_stage': False,      # second aggressive pass for big pops
    'declip': False,         # repair clipped/overdriven peaks (expensive)

    # --- denoiser --------------------------------------------------------------
    'strength': 12.0,        # noise reduction in dB, 1..30
    # Measured as a no-op on ffmpeg 6.1, so the floor is set below instead.
    'track_noise': False,
    # Level-based estimates drift up into the music; a fixed floor is safer.
    'auto_noise_floor': False,
    'noise_floor': -60.0,    # dB, updated automatically when auto is on

    # --- pre-stage (applies to every path, filtered or not) --------------------
    'rumble': True,          # high-pass out warp and turntable rumble
    'rumble_hz': 18,
    'bass_mono': True,       # elliptical filter: mono below this, phase-coherent
    'bass_mono_hz': 150,

    # --- output ----------------------------------------------------------------
    'wet': 1.0,              # 0..1 blend between pre-stage audio and processed
}

_lock = threading.Lock()
_timer = None


def _apply_env(data):
    """Apply VINYL_<KEY> overrides, coerced to the type of each default."""
    for key, default in DEFAULTS.items():
        raw = os.environ.get("VINYL_" + key.upper())
        if raw is None:
            continue
        try:
            if isinstance(default, bool):
                data[key] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, list):
                # Semicolons: ALSA device names contain commas (hw:1,0).
                data[key] = [v.strip() for v in raw.split(";") if v.strip()]
            elif isinstance(default, int):
                data[key] = int(raw)
            elif isinstance(default, float):
                data[key] = float(raw)
            else:
                data[key] = raw
        except ValueError:
            print(f"Settings: ignoring bad VINYL_{key.upper()}={raw!r}", flush=True)
    return data


def load():
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH) as f:
            saved = json.load(f)
        data.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Settings: could not read {SETTINGS_PATH}, using defaults ({e})", flush=True)
    return _apply_env(data)


def _write(data):
    tmp = SETTINGS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def save_soon(data, delay=1.0):
    """Coalesce bursts of changes (knob drags, switch flicks) into one write."""
    global _timer
    snapshot = {k: data[k] for k in DEFAULTS}
    with _lock:
        if _timer:
            _timer.cancel()
        _timer = threading.Timer(delay, _write, args=(snapshot,))
        _timer.daemon = True
        _timer.start()
