"""
Realtime audio engine: input -> pre-stage / declick / denoise -> output.

Playback runs behind the input so ffmpeg has time to deliver. Anything not
ready in time is filled with dry input rather than silence. The front panel
selects the variant; the noise floor and declick sensitivity are measured here.
"""
import collections
import shutil
import threading
import time

import numpy as np
import sounddevice as sd

import settings
from dsp import Processor, Timeline

CHANNELS = 2
FADE_MS = 15
_UNSET = object()   # "no previous block yet", distinct from a variant name

SAMPLE_RATES = (44100, 48000, 88200, 96000)
BUFFER_SIZES = (0, 512, 1024, 2048, 4096)

SPEC_SIZE = 1024          # FFT length for the spectrograms
SPEC_BINS = 48            # log-spaced display bands
SPEC_HISTORY = 48         # columns kept for clients to catch up on

# Auto-tuner behaviour
TUNE_PERIOD = 5.0         # how often the tuner thread looks at the numbers
FLOOR_MIN_INTERVAL = 30.0
FLOOR_MIN_CHANGE = 3.0      # dB
FLOOR_MARGIN = 3.0          # sit just above the measured floor
FLOOR_MIN_HEADROOM = 30.0   # programme must be this far above it to be believable
FLOOR_CEILING = -50.0       # never higher: measured -43 dB already gutted a record
# Removing this much of the programme pulls the floor back down immediately.
DESTRUCTION_RATIO_DB = -20.0
SENS_MIN_INTERVAL = 45.0
SENS_TOO_MUCH_DB = -35.0  # removed signal this close to the music: back off
SENS_TOO_LITTLE_DB = -55.0

# Expensive options are shed automatically if ffmpeg falls behind.
OVERLOAD_LAG_FRACTION = 0.75   # of the latency budget
OVERLOAD_SECONDS = 10.0
SHEDDABLE = ('declip', 'two_stage')


class Engine:
    def __init__(self):
        self.opts = settings.load()
        self.monitor = False            # never persisted: booting into "noise only" would be confusing
        self.running = False
        self.error = None
        self.xruns = 0
        self.panel = {'connected': False, 'switches': None, 'raw': None}
        # wpctl only exists under PipeWire; without it the UI hides the control.
        self.volume_available = shutil.which('wpctl') is not None
        self.device_latency_ms = [0.0, 0.0]   # input, output; filled in at start()
        self.auto_state = {'noise_floor': None, 'sensitivity': None, 'reason': 'waiting for audio'}

        self._lock = threading.RLock()
        self._stream = None
        self._proc = None
        self._dry = None
        self._index = 0
        self._latency = 0
        # Fractional read position: playing slow builds delay, fast sheds it.
        self._play = 0.0
        self._rate = 1.0
        self._rate_target = 1.0
        self._idle = True
        self._bufs = {}
        self._last_variant = _UNSET
        self._fallback = 0
        self._meters = np.zeros(3, dtype=np.float32)  # in, out, removed (peak)
        self._rms_sum = np.zeros(2, dtype=np.float64)  # in, out (energy, for the VU needle)
        self._rms_n = 0
        self._devices = None

        # spectrum
        self._spec_acc = np.zeros((SPEC_SIZE, 2), dtype=np.float32)   # program, removed
        self._spec_fill = 0
        self._spec_seq = 0
        self._spec_cols = collections.deque(maxlen=SPEC_HISTORY)
        self._spec_window = np.hanning(SPEC_SIZE).astype(np.float32)
        self._spec_edges = None

        # auto-tuner statistics, written by the callback and read by the tuner
        self._block_levels = collections.deque(maxlen=2000)   # dBFS per block
        self._removed_ratio = collections.deque(maxlen=2000)  # removed vs program, dB
        self._last_floor_change = 0.0
        self._last_sens_change = 0.0
        self._overload_for = 0.0

        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._auto_tune, daemon=True).start()

    # -- devices -------------------------------------------------------------

    def devices(self, refresh=False):
        if self._devices is None or refresh:
            inputs, outputs = [], []
            for d in sd.query_devices():
                if d['max_input_channels'] >= CHANNELS:
                    inputs.append(d['name'])
                if d['max_output_channels'] >= CHANNELS:
                    outputs.append(d['name'])
            self._devices = {'inputs': inputs, 'outputs': outputs}
        return self._devices

    def _resolve(self, name, kind):
        key = 'max_input_channels' if kind == 'input' else 'max_output_channels'
        devs = list(enumerate(sd.query_devices()))
        usable = [(i, d['name']) for i, d in devs if d[key] >= CHANNELS]
        if not usable:
            raise RuntimeError(f"No usable {kind} device found")
        # 1. the saved device, by exact name
        for i, n in usable:
            if name and n == name:
                return i, n
        # 2. the first name containing a preferred fragment, in order
        for want in self.opts.get('preferred_devices') or []:
            for i, n in usable:
                if want.lower() in n.lower():
                    return i, n
        # 3. anything usable
        return usable[0]

    # -- lifecycle -------------------------------------------------------------

    def start(self):
        with self._lock:
            if self._stream is not None:
                return
            o = self.opts
            try:
                in_idx, in_name = self._resolve(o['input_device'], 'input')
                out_idx, out_name = self._resolve(o['output_device'], 'output')
                sr = int(o['sample_rate'])
                self._latency = int(sr * o['latency_ms'] / 1000)
                self._dry = Timeline(1 << int(np.ceil(np.log2(sr * 2))), CHANNELS, 0)
                self._index = 0
                self._play = -float(self._latency)   # first `latency` samples are silence, as before
                self._rate = self._rate_target = 1.0
                self._last_variant = _UNSET
                self._spec_edges = np.unique(np.geomspace(
                    max(1, int(30 / (sr / SPEC_SIZE))), SPEC_SIZE // 2, SPEC_BINS + 1).astype(int))
                self._idle = not (o['declicker_on'] or o['denoiser_on'])
                self._proc = None if (self._idle and o['idle_ffmpeg']) \
                    else Processor(sr, CHANNELS, o, self._latency)
                self._stream = sd.Stream(device=(in_idx, out_idx), samplerate=sr,
                                         blocksize=int(o['buffer_size']), channels=CHANNELS,
                                         dtype='float32', callback=self._callback)
                self._stream.start()
                # Host-reported latency, for end-to-end figures.
                lat = self._stream.latency
                self.device_latency_ms = [round(v * 1000, 1) for v in
                                          (lat if isinstance(lat, (list, tuple)) else (lat, lat))]
                self.running = True
                self.error = None
                print(f"Audio: {in_name} -> {out_name} @ {sr} Hz, block {o['buffer_size'] or 'auto'}, "
                      f"delay {o['latency_ms']} ms", flush=True)
            except Exception as e:
                self.error = str(e)
                print(f"Audio: failed to start stream: {e}", flush=True)
                self._teardown()

    def stop(self):
        with self._lock:
            self._teardown()

    def restart(self):
        with self._lock:
            self._teardown()
            time.sleep(0.3)
            self.start()

    def _teardown(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.close()
        self.running = False

    def _watchdog(self):
        # Keep retrying if the device isn't ready at boot or disappears later.
        time.sleep(2)
        while True:
            with self._lock:
                stream = self._stream
                if stream is None or not stream.active:
                    if stream is not None:
                        print("Audio: stream stopped unexpectedly, restarting", flush=True)
                        self._teardown()
                    self.start()
                elif self.opts['idle_ffmpeg'] and self._idle and self._proc is not None:
                    # Delay has collapsed, so no variant will be read again.
                    sr = self.opts['sample_rate']
                    idle_target = sr * self.opts['idle_latency_ms'] / 1000
                    if (self._index - self._play) <= idle_target + sr * 0.02:
                        proc, self._proc = self._proc, None
                        proc.close()
                        print("DSP: nothing engaged, ffmpeg stopped", flush=True)
            time.sleep(5)

    # -- options ---------------------------------------------------------------

    BOOL_KEYS = ('two_stage', 'declip', 'track_noise', 'auto_noise_floor',
                 'auto_sensitivity', 'rumble', 'bass_mono', 'idle_ffmpeg')
    NUM_KEYS = {
        'sensitivity': (0.0, 10.0, 1),
        'strength': (1.0, 30.0, 1),
        'noise_floor': (-80.0, -20.0, 0),
        'wet': (0.0, 1.0, 2),
        'rumble_hz': (5, 100, 0),
        'bass_mono_hz': (50, 400, 0),
        # Delay targets and travel rates; applied without a restart.
        'latency_ms': (80, 1000, 0),
        'idle_latency_ms': (5, 200, 0),
        'engage_rate_pct': (0.1, 5.0, 2),
        'release_rate_pct': (0.1, 5.0, 2),
    }

    def set_options(self, changes, from_panel=False):
        """
        Apply a dict of option changes. Returns the resulting snapshot.

        Declicker, denoiser and monitor belong to the physical switches: while
        the panel is connected the web UI only mirrors them, so changes to
        those keys are ignored unless they come from the panel itself. If the
        panel is unplugged they can be set over the API as a fallback.
        """
        restart = False
        with self._lock:
            o = self.opts
            if from_panel or not self.panel['connected']:
                for key in ('declicker_on', 'denoiser_on'):
                    if key in changes:
                        o[key] = bool(changes[key])
                if 'monitor' in changes:
                    self.monitor = bool(changes['monitor'])

            for key in self.BOOL_KEYS:
                if key in changes:
                    o[key] = bool(changes[key])
            for key, (lo, hi, digits) in self.NUM_KEYS.items():
                if key in changes:
                    value = min(hi, max(lo, float(changes[key])))
                    o[key] = round(value, digits) if digits else int(round(value))

            if 'sample_rate' in changes and int(changes['sample_rate']) in SAMPLE_RATES:
                restart |= int(changes['sample_rate']) != o['sample_rate']
                o['sample_rate'] = int(changes['sample_rate'])
            if 'buffer_size' in changes and int(changes['buffer_size']) in BUFFER_SIZES:
                restart |= int(changes['buffer_size']) != o['buffer_size']
                o['buffer_size'] = int(changes['buffer_size'])
            for key in ('input_device', 'output_device'):
                if key in changes and changes[key] != o[key]:
                    o[key] = changes[key] or None
                    restart = True

            # Keep the processing latency the engine and processor agree on.
            self._latency = int(o['sample_rate'] * o['latency_ms'] / 1000)
            if self._proc is not None:
                self._proc.latency_samples = self._latency
            if self._proc is not None and not restart:
                self._proc.set_config(o)
            self._sync_processor()
            settings.save_soon(o)
        if restart:
            self.restart()
        return self.snapshot()

    def _sync_processor(self):
        """
        Start ffmpeg the moment a filter is engaged, so it warms up and aligns
        while the delay is still being built. Stopping is left to the watchdog,
        which waits until the delay has actually collapsed.
        """
        o = self.opts
        if not self.running or self._stream is None:
            return
        if (o['declicker_on'] or o['denoiser_on']) and self._proc is None:
            try:
                self._proc = Processor(int(o['sample_rate']), CHANNELS, o, self._latency)
                print("DSP: filter engaged, starting ffmpeg", flush=True)
            except Exception as e:
                print(f"DSP: could not start ffmpeg: {e}", flush=True)

    # -- realtime ----------------------------------------------------------------

    def _buf(self, name, frames):
        b = self._bufs.get(name)
        if b is None or b.shape[0] != frames:
            b = self._bufs[name] = np.zeros((frames, CHANNELS), dtype=np.float32)
        return b

    def _callback(self, indata, outdata, frames, time_info, status):
        if status.input_overflow or status.output_underflow:
            self.xruns += 1

        dry_tl, proc = self._dry, self._proc
        if dry_tl is None:
            outdata.fill(0)
            return
        # proc is None while bypassed: ffmpeg is stopped entirely.

        index = self._index
        dry_tl.write(indata)
        if proc is not None:
            proc.feed(indata, index)
        self._index = index + frames

        o = self.opts
        dc, dn = o['declicker_on'], o['denoiser_on']
        self._idle = not (dc or dn)
        rate = self._update_rate(self._index, frames)

        dry = self._buf('dry', frames)
        if not self._read_dry(dry, frames, rate):
            # Should not happen with the playhead clamped; quieter than silence.
            dry[:] = self._buf('hold', frames)

        variant = None if self._idle else ('both' if dc and dn else 'dc' if dc else 'dn')
        # `pre` is the reference for the wet/dry blend and the removed signal.
        pre = dry if variant is None else self._read_variant('pre', frames, rate, dry, 'pre')
        mixed = pre if variant is None else \
            self._blend(self._read_variant(variant, frames, rate, dry, 'clean'), pre, frames)

        prev = self._last_variant
        if prev is not _UNSET and prev != variant:
            # Short crossfade when switching so the change never ticks.
            before = dry if prev is None else \
                self._blend(self._read_variant(prev, frames, rate, dry, 'prev'), pre, frames)
            fade = max(1, min(frames, int(o['sample_rate'] * FADE_MS / 1000)))
            ramp = np.ones(frames, dtype=np.float32)
            ramp[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            mixed = before + (mixed - before) * ramp[:, None]
        self._last_variant = variant

        removed = pre - mixed
        outdata[:] = removed if self.monitor else mixed
        self._buf('hold', frames)[:] = dry
        self._play += frames * rate

        m = self._meters
        in_peak = float(np.abs(dry).max())
        m[0] = max(m[0], in_peak)
        m[1] = max(m[1], float(np.abs(outdata).max()))
        m[2] = max(m[2], float(np.abs(removed).max()))
        # VU is RMS; peak is what clips.
        self._rms_sum[0] += float(np.sum(dry.astype(np.float64) ** 2))
        self._rms_sum[1] += float(np.sum(np.asarray(outdata, dtype=np.float64) ** 2))
        self._rms_n += frames * CHANNELS

        self._collect_stats(dry, mixed, removed, frames)

    # -- variable-rate playback --------------------------------------------------

    def _update_rate(self, index, frames):
        """
        Steer the delay toward its target by playing fractionally fast or slow.
        The rate itself is ramped, so pitch glides rather than stepping.
        """
        o = self.opts
        sr = o['sample_rate']
        target = int(sr * (o['idle_latency_ms'] if self._idle else o['latency_ms']) / 1000)
        # Delay as it will stand at the end of this block: the playhead advances
        # about `frames` while it plays, so measuring at the start lands short.
        err = (index - self._play - frames) - target   # positive: too much delay
        tol = sr * 0.002
        if err > tol:
            self._rate_target = 1.0 + float(o['release_rate_pct']) / 100.0
        elif err < -tol:
            self._rate_target = 1.0 - float(o['engage_rate_pct']) / 100.0
        else:
            self._rate_target = 1.0
        # ~200 ms glide onto the new rate
        alpha = min(1.0, frames / (0.2 * sr))
        self._rate += (self._rate_target - self._rate) * alpha
        # Never overshoot the target inside one block.
        if err > 0:
            self._rate = min(self._rate, 1.0 + max(0.0, err) / frames)
        elif err < 0:
            self._rate = max(self._rate, 1.0 - max(0.0, -err) / frames)

        # Keep the playhead behind the newest input (interpolation reads three
        # samples ahead) and inside the ring buffer. A floor of a whole block
        # would sit above the idle target and never be reached.
        min_delay = 16
        max_delay = self._dry.cap - 4 * frames
        end = self._play + frames * self._rate
        if index - end < min_delay:
            self._rate = (index - min_delay - self._play) / frames
        elif index - end > max_delay:
            self._rate = (index - max_delay - self._play) / frames
        self._rate = float(min(1.1, max(0.9, self._rate)))
        return self._rate

    def _resample(self, src, frames, rate, out):
        """Catmull-Rom interpolation of `src` at play + k*rate (src starts at floor(play)-1)."""
        base = self._play - (np.floor(self._play) - 1)
        t = base + np.arange(frames, dtype=np.float64) * rate
        idx = t.astype(np.int64)
        f = (t - idx)[:, None].astype(np.float32)
        y0, y1, y2, y3 = src[idx - 1], src[idx], src[idx + 1], src[idx + 2]
        a = -0.5 * y0 + 1.5 * y1 - 1.5 * y2 + 0.5 * y3
        b = y0 - 2.5 * y1 + 2.0 * y2 - 0.5 * y3
        c = -0.5 * y0 + 0.5 * y2
        out[:] = ((a * f + b) * f + c) * f + y1

    def _span(self, frames, rate):
        return int(np.floor(self._play)) - 1, int(np.ceil(frames * rate)) + 4

    def _read_dry(self, out, frames, rate):
        i0, n = self._span(frames, rate)
        src = self._buf('src_dry', n)
        src.fill(0)
        lo, hi = self._dry.read_into(i0, src)
        if hi - lo < n:
            self._fallback += frames
            return False
        self._resample(src, frames, rate, out)
        return True

    def _read_variant(self, variant, frames, rate, dry, name):
        """Resampled processed audio, falling back to dry for the whole block."""
        out = self._buf(name, frames)
        i0, n = self._span(frames, rate)
        src = self._buf('src_' + name, n)
        src.fill(0)
        lo, hi = self._proc.read_into(variant, i0, src) if self._proc else (0, 0)
        if hi - lo < n:
            self._fallback += frames
            out[:] = dry
            return out
        self._resample(src, frames, rate, out)
        return out

    def _blend(self, processed, pre, frames):
        wet = float(self.opts['wet'])
        if wet >= 0.999:
            return processed
        return pre + (processed - pre) * wet

    # -- measurement ---------------------------------------------------------------

    def _collect_stats(self, dry, mixed, removed, frames):
        """Feed the auto-tuners and the spectrograms. Cheap enough for the callback."""
        prog_rms = float(np.sqrt(np.mean(mixed.astype(np.float32) ** 2)) + 1e-12)
        rem_rms = float(np.sqrt(np.mean(removed.astype(np.float32) ** 2)) + 1e-12)
        self._block_levels.append(20 * np.log10(float(np.sqrt(np.mean(dry ** 2)) + 1e-12)))
        if prog_rms > 10 ** (-50 / 20):
            self._removed_ratio.append(20 * np.log10(rem_rms / prog_rms))

        # accumulate mono frames until we have a full FFT window
        take = min(frames, SPEC_SIZE - self._spec_fill)
        acc = self._spec_acc
        acc[self._spec_fill:self._spec_fill + take, 0] = mixed[:take].mean(axis=1)
        acc[self._spec_fill:self._spec_fill + take, 1] = removed[:take].mean(axis=1)
        self._spec_fill += take
        if self._spec_fill >= SPEC_SIZE:
            self._spec_fill = 0
            edges = self._spec_edges
            if edges is not None:
                col = {}
                for i, key in enumerate(('out', 'removed')):
                    mag = np.abs(np.fft.rfft(acc[:, i] * self._spec_window)) / (SPEC_SIZE / 4)
                    bands = [float(mag[edges[b]:edges[b + 1]].max() if edges[b + 1] > edges[b] else mag[edges[b]])
                             for b in range(len(edges) - 1)]
                    col[key] = [round(float(20 * np.log10(v + 1e-6)), 1) for v in bands]
                self._spec_seq += 1
                col['seq'] = self._spec_seq
                self._spec_cols.append(col)

    def _auto_tune(self):
        """
        Hands-off tuning: follow the record's own noise floor, and keep the
        declicker removing a sensible amount rather than eating the music.
        Each change is applied by the processor warming up a new ffmpeg, so
        retuning is inaudible.
        """
        while True:
            time.sleep(TUNE_PERIOD)
            try:
                self._tune_once()
            except Exception as e:
                print(f"Auto: tuner error: {e}", flush=True)

    def _tune_once(self):
        """One tuning pass. Split from the loop so the tests can drive it."""
        if not self.running or self._proc is None:
            return
        o = self.opts
        now = time.monotonic()
        levels = list(self._block_levels)
        ratios = list(self._removed_ratio)
        changes = {}
        reason = []

        # --- overload guard: is ffmpeg keeping up? ---------------------
        lag = self._proc.lag_samples(self._index)
        lag_ms = None if lag is None else lag / o['sample_rate'] * 1000
        if lag_ms is not None and lag_ms > OVERLOAD_LAG_FRACTION * o['latency_ms']:
            self._overload_for += TUNE_PERIOD
        else:
            self._overload_for = 0.0
            self.auto_state.pop('overload', None)
        if self._overload_for >= OVERLOAD_SECONDS:
            self._overload_for = 0.0
            for key in SHEDDABLE:
                if o[key]:
                    changes[key] = False
                    msg = f"{key.replace('_', ' ')} turned off: DSP was {lag_ms:.0f} ms behind"
                    break
            else:
                msg = (f"DSP is {lag_ms:.0f} ms behind on the basic chain; "
                       f"try a larger buffer or a lower sample rate")
            self.auto_state['overload'] = msg
            print("Auto: " + msg, flush=True)

        if len(levels) > 1000:
            # A floor is what a gap sounds like, so take a low order
            # statistic and only trust it well below the programme.
            floor = float(np.percentile(levels, 2))
            loud = float(np.percentile(levels, 90))
            self.auto_state['noise_floor'] = round(floor, 1)
            usable = loud > -60 and (loud - floor) >= FLOOR_MIN_HEADROOM
            self.auto_state['floor_usable'] = usable
            if o['auto_noise_floor'] and usable:
                target = float(np.clip(floor + FLOOR_MARGIN, -80, FLOOR_CEILING))
                if abs(target - o['noise_floor']) >= FLOOR_MIN_CHANGE \
                        and now - self._last_floor_change > FLOOR_MIN_INTERVAL:
                    changes['noise_floor'] = target
                    self._last_floor_change = now
                    reason.append(f"noise floor -> {target:.0f} dB")

        # --- destruction guard: are the filters eating the music? -----
        if ratios and o['denoiser_on']:
            recent = float(np.percentile(ratios[-400:], 50))
            if recent > DESTRUCTION_RATIO_DB and o['noise_floor'] > -75:
                target = float(max(-75.0, o['noise_floor'] - 5.0))
                changes['noise_floor'] = target
                changes['auto_noise_floor'] = False
                msg = (f"removing {recent:.0f} dB of the programme: noise floor "
                       f"pulled down to {target:.0f} dB and auto floor disabled")
                self.auto_state['overload'] = msg
                print("Auto: " + msg, flush=True)

        if o['auto_sensitivity'] and o['declicker_on'] and len(ratios) > 200:
            ratio = float(np.percentile(ratios, 75))
            self.auto_state['sensitivity'] = round(ratio, 1)
            if now - self._last_sens_change > SENS_MIN_INTERVAL:
                step = 0.0
                if ratio > SENS_TOO_MUCH_DB:
                    step = -0.5       # cutting into the music
                elif ratio < SENS_TOO_LITTLE_DB:
                    step = 0.5        # barely doing anything
                target = float(np.clip(o['sensitivity'] + step, 0.0, 10.0))
                if step and target != o['sensitivity']:
                    changes['sensitivity'] = target
                    self._last_sens_change = now
                    reason.append(f"sensitivity -> {target:.1f} ({ratio:.0f} dB removed)")

        if changes:
            self.auto_state['reason'] = "; ".join(reason)
            print("Auto: " + self.auto_state['reason'], flush=True)
            self.set_options(changes)
        elif not self.auto_state.get('reason'):
            self.auto_state['reason'] = 'steady'

    # -- reporting -----------------------------------------------------------------

    def meters(self, since=None):
        m = self._meters.copy()
        self._meters[:] = 0
        to_db = lambda v: round(20 * np.log10(max(float(v), 1e-5)), 1)
        rms, n = self._rms_sum.copy(), self._rms_n
        self._rms_sum[:] = 0
        self._rms_n = 0
        rms_db = [to_db(np.sqrt(v / n)) if n else -100.0 for v in rms]
        out = {'in': to_db(m[0]), 'out': to_db(m[1]), 'removed': to_db(m[2]),
               'in_rms': rms_db[0], 'out_rms': rms_db[1], 'seq': self._spec_seq}
        # Energy ratio, not peak: one repaired click is a large peak and inaudible.
        ratios = list(self._removed_ratio)[-200:]
        out['removed_ratio'] = round(float(np.median(ratios)), 1) if ratios else None
        if since is not None:
            out['spectrum'] = [c for c in list(self._spec_cols) if c['seq'] > since]
        return out

    def snapshot(self):
        o = self.opts
        proc = self._proc
        lag = proc.lag_samples(self._index) if proc else None
        state = {
            'running': self.running,
            'error': self.error,
            'monitor': self.monitor,
            'dsp': {
                'lag_ms': None if lag is None else round(lag / o['sample_rate'] * 1000),
                'restarts': proc.restarts if proc else 0,
                'swaps': proc.swaps if proc else 0,
                'fallback_ms': round(self._fallback / o['sample_rate'] * 1000),
                'aligned': bool(proc and proc.aligned),
                'offsets_ms': proc.offsets_ms() if proc else None,
            },
            'auto': dict(self.auto_state),
            'xruns': self.xruns,
            'panel': self.panel,
            'volume_control': self.volume_available,
            'device_latency_ms': self.device_latency_ms,
            'delay_ms': round(max(0.0, self._index - self._play) / o['sample_rate'] * 1000),
            'speed_pct': round((self._rate - 1.0) * 100, 2),
            'idle': self._idle,
        }
        state.update({k: o[k] for k in (
            'input_device', 'output_device', 'sample_rate', 'buffer_size', 'latency_ms',
            'declicker_on', 'denoiser_on', 'sensitivity', 'strength', 'noise_floor',
            'two_stage', 'declip', 'track_noise', 'auto_noise_floor', 'auto_sensitivity',
            'rumble', 'rumble_hz', 'bass_mono', 'bass_mono_hz', 'wet',
            'idle_latency_ms', 'engage_rate_pct', 'release_rate_pct', 'idle_ffmpeg')})
        return state
