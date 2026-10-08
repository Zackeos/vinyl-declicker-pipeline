import re
import subprocess

from flask import Flask, jsonify, request, send_from_directory

import hardware_input
from audio_engine import Engine

app = Flask(__name__)
engine = Engine()
hardware_input.start_listener(engine)

# The engine refuses the three switch keys while the panel is connected.
OPTION_KEYS = ('declicker_on', 'denoiser_on', 'monitor',
               'sensitivity', 'strength', 'noise_floor', 'wet',
               'two_stage', 'declip', 'track_noise', 'auto_noise_floor', 'auto_sensitivity',
               'rumble', 'rumble_hz', 'bass_mono', 'bass_mono_hz',
               'latency_ms', 'idle_latency_ms', 'engage_rate_pct', 'release_rate_pct', 'idle_ffmpeg',
               'input_device', 'output_device', 'sample_rate', 'buffer_size')


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.get('/api/state')
def get_state():
    return jsonify(engine.snapshot())


@app.post('/api/state')
def update_state():
    data = request.get_json(silent=True) or {}
    changes = {k: data[k] for k in OPTION_KEYS if k in data}
    try:
        return jsonify(engine.set_options(changes))
    except (TypeError, ValueError) as e:
        return jsonify({'error': f'Invalid value: {e}'}), 400


@app.get('/api/devices')
def get_devices():
    return jsonify(engine.devices(refresh=request.args.get('refresh') == '1'))


@app.get('/api/meters')
def get_meters():
    """Peak levels, plus any spectrogram columns newer than `since`."""
    since = request.args.get('since', type=int)
    return jsonify(engine.meters(since=since))


def _no_volume_control():
    return jsonify({'error': 'Volume control needs PipeWire (wpctl), which is not installed',
                    'available': False}), 503


@app.get('/api/volume')
def get_volume():
    """Master volume of the default sink, via wpctl."""
    if not engine.volume_available:
        return _no_volume_control()
    try:
        result = subprocess.run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                                capture_output=True, text=True, check=True, timeout=3)
        match = re.search(r'Volume:\s*(\d+\.\d+)', result.stdout)
        if not match:
            return jsonify({'error': 'Could not parse volume from wpctl'}), 502
        return jsonify({'volume': float(match.group(1)), 'muted': '[MUTED]' in result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.post('/api/volume')
def set_volume():
    if not engine.volume_available:
        return _no_volume_control()
    data = request.get_json(silent=True) or {}
    try:
        vol = max(0.0, min(1.0, float(data['volume'])))
        subprocess.run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{vol:.2f}'], check=True, timeout=3)
        return jsonify({'volume': vol})
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'No valid volume provided'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 502
