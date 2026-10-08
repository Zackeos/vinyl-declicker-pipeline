"""
FFmpeg-backed declick and denoise.

One long-running ffmpeg renders four variants of the input in parallel (pre,
dc, dn, both), so the switches select between them without restarting
anything. Outputs go into ring buffers keyed by absolute input sample.

The outputs do not all start on the same sample, so each instance is fed
silence plus a chirp before the live audio, and the chirp is located in each
output to measure its offset. Filter settings cannot be changed on a running
ffmpeg, so a change starts a second one and crossfades onto it.
"""
import os
import queue
import subprocess
import threading
import time

import numpy as np

VARIANTS = ('pre', 'dc', 'dn', 'both')
MARKER_SECS = 0.05
# Leading silence, so whatever ffmpeg trims or pads at startup comes out of the
# silence and the marker stays fully visible.
PAD_SECS = 0.15
SWAP_FADE_SECS = 0.02

CHAIN_KEYS = ('sensitivity', 'two_stage', 'declip', 'strength', 'noise_floor',
              'track_noise', 'rumble', 'rumble_hz', 'bass_mono', 'bass_mono_hz')

_marker_cache = {}


def build_marker(sample_rate, channels):
    """
    Returns (preroll, marker): `preroll` is silence followed by a windowed
    120 Hz -> 9 kHz chirp, and is what gets fed to ffmpeg. The chirp is
    broadband and deterministic, so it correlates to a single sharp peak.
    """
    key = (sample_rate, channels)
    if key not in _marker_cache:
        n = int(MARKER_SECS * sample_rate)
        t = np.arange(n) / sample_rate
        f0, f1, dur = 120.0, 9000.0, n / sample_rate
        sweep = np.sin(2 * np.pi * (f0 * t + (f1 - f0) / (2 * dur) * t ** 2))
        # Quiet on purpose: correlation stays sharp, and if it ever leaks to the
        # speakers it is barely audible rather than a chirp in the middle of a side.
        mono = (0.08 * sweep * np.hanning(n)).astype(np.float32)
        marker = np.ascontiguousarray(np.tile(mono[:, None], (1, channels)))
        pad = np.zeros((int(PAD_SECS * sample_rate), channels), dtype=np.float32)
        _marker_cache[key] = (np.ascontiguousarray(np.vstack((pad, marker))), marker)
    return _marker_cache[key]


def declick_threshold(sensitivity):
    # Sensitivity 0 (gentle) .. 10 (aggressive) -> adeclick threshold 8.0 .. 1.5
    return max(1.5, 8.0 - float(sensitivity) * 0.65)


def build_graph(cfg):
    """Build the filter_complex for the four variants from a settings dict."""
    # --- pre-stage: rumble high-pass, then elliptical bass mono ---------------
    pre_parts = []
    node = '[0:a]'
    if cfg.get('rumble'):
        pre_parts.append(f"{node}highpass=f={int(cfg['rumble_hz'])}:poles=2[r]")
        node = '[r]'
    if cfg.get('bass_mono'):
        # High-pass the side channel only: mono below the corner, phase-coherent,
        # and an exact reconstruction above it.
        hz = int(cfg['bass_mono_hz'])
        pre_parts.append(f"{node}asplit=2[rm][rs]")
        pre_parts.append("[rm]pan=mono|c0=0.5*c0+0.5*c1[mid]")
        pre_parts.append(f"[rs]pan=mono|c0=0.5*c0-0.5*c1,highpass=f={hz}:poles=2[side]")
        pre_parts.append("[mid][side]amerge=inputs=2,pan=stereo|c0=c0+c1|c1=c0-c1[pre0]")
    else:
        pre_parts.append(f"{node}anull[pre0]")

    # --- declick chain ---------------------------------------------------------
    gentle = declick_threshold(cfg['sensitivity'])
    chain = [f"adeclick=window=55:overlap=50:arorder=2:threshold={gentle:.2f}:burst=2:method=a"]
    if cfg.get('two_stage'):
        # A second, more aggressive pass aimed at isolated big pops rather than
        # the continuous crackle the first pass handles. Keep burst and arorder
        # modest: on quiet passages adeclick finds impulses everywhere, and the
        # repair cost scales with both.
        pops = max(1.0, gentle * 0.45)
        chain.append(f"adeclick=window=55:overlap=50:arorder=2:threshold={pops:.2f}:burst=4:method=a")
    if cfg.get('declip'):
        chain.insert(0, "adeclip=window=55:overlap=75:arorder=8:threshold=10:hsize=1000:method=add")
    declick = ','.join(chain)

    # --- denoiser --------------------------------------------------------------
    nf = float(np.clip(cfg.get('noise_floor', -50.0), -80, -20))
    tn = 1 if cfg.get('track_noise') else 0
    denoise = f"afftdn=nr={float(cfg['strength']):.2f}:nf={nf:.1f}:tn={tn}"

    return (';'.join(pre_parts) + ';'
            "[pre0]asplit=3[pre][a][b];"
            f"[a]{declick},asplit=2[dc][dc2];"
            f"[dc2]{denoise}[both];"
            f"[b]{denoise}[dn]")


class Timeline:
    """Fixed-size stereo ring buffer addressed by absolute sample index."""

    def __init__(self, capacity, channels, start):
        self.cap = int(capacity)
        self.buf = np.zeros((self.cap, channels), dtype=np.float32)
        self.start = start    # first absolute index ever written
        self.end = start      # one past the last absolute index written

    def write(self, data):
        n = data.shape[0]
        if n > self.cap:
            data = data[-self.cap:]
            self.end += n - self.cap
            n = self.cap
        pos = self.end % self.cap
        first = min(n, self.cap - pos)
        self.buf[pos:pos + first] = data[:first]
        if first < n:
            self.buf[:n - first] = data[first:]
        self.end += n

    def read_into(self, start, out):
        """
        Copy samples [start, start + len(out)) into `out` where available.
        Returns (lo, hi): the offsets within `out` that were filled.
        """
        n = out.shape[0]
        end = self.end  # snapshot; the writer thread may advance it
        lo = max(start, self.start, end - self.cap)
        hi = min(start + n, end)
        if hi <= lo:
            return 0, 0
        pos = lo % self.cap
        count = hi - lo
        first = min(count, self.cap - pos)
        o = lo - start
        out[o:o + first] = self.buf[pos:pos + first]
        if first < count:
            out[o + first:o + count] = self.buf[:count - first]
        return o, o + count


class _Instance:
    """One running ffmpeg process, its pipes and its timelines."""

    def __init__(self, sample_rate, channels, cfg, capacity):
        self.sample_rate = sample_rate
        self.channels = channels
        self.cfg = dict(cfg)
        self.capacity = capacity
        self.base = None         # first live input sample index handed to ffmpeg
        self.preroll, self.marker = build_marker(sample_rate, channels)
        self.timelines = {}
        self.offsets = {}        # variant -> sample shift of its output stream
        self.aligned = False
        self.dead = False
        self.inbox = queue.Queue(maxsize=64)

        read_fds, write_fds = {}, {}
        for v in VARIANTS:
            r, w = os.pipe()
            read_fds[v], write_fds[v] = r, w

        # probesize/analyzeduration keep ffmpeg from buffering seconds of input
        # before it starts filtering; avioflags direct stops it buffering output.
        cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error',
               '-fflags', 'nobuffer', '-flags', 'low_delay',
               '-probesize', '32', '-analyzeduration', '0',
               '-f', 'f32le', '-ar', str(sample_rate),
               '-ac', str(channels), '-i', 'pipe:0', '-filter_complex', build_graph(cfg)]
        for v in VARIANTS:
            cmd += ['-map', f'[{v}]', '-f', 'f32le', '-avioflags', 'direct',
                    '-flush_packets', '1', f'pipe:{write_fds[v]}']

        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, pass_fds=tuple(write_fds.values()),
                                     bufsize=0)
        for w in write_fds.values():
            os.close(w)

        threads = [threading.Thread(target=self._writer, daemon=True),
                   threading.Thread(target=self._stderr, daemon=True)]
        threads += [threading.Thread(target=self._reader, args=(v, fd), daemon=True)
                    for v, fd in read_fds.items()]
        for t in threads:
            t.start()

    def begin(self, index):
        """
        Called from the audio callback before this instance's first live block.
        The preroll occupies input indices [index - len(preroll), index), so an
        unshifted output stream starts at index - len(preroll).
        """
        for v in VARIANTS:
            self.timelines[v] = Timeline(self.capacity, self.channels, index - self.preroll.shape[0])
        self.base = index
        self.feed(self.preroll)

    def feed(self, block):
        try:
            self.inbox.put_nowait(block.tobytes())
        except queue.Full:
            # ffmpeg stalled; dropping input would break alignment.
            self.dead = True

    def _writer(self):
        try:
            while not self.dead:
                chunk = self.inbox.get()
                if chunk is None:
                    break
                self.proc.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            self.dead = True
            try:
                self.proc.stdin.close()
            except Exception:
                pass

    def _reader(self, variant, fd):
        frame_bytes = 4 * self.channels
        pending = b''
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                pending += chunk
                usable = len(pending) - len(pending) % frame_bytes
                if usable:
                    samples = np.frombuffer(pending[:usable], dtype=np.float32).reshape(-1, self.channels)
                    self.timelines[variant].write(samples)
                    pending = pending[usable:]
        except OSError:
            pass
        finally:
            os.close(fd)
            self.dead = True

    def _stderr(self):
        for line in self.proc.stderr:
            text = line.decode(errors='replace').strip()
            if text:
                print(f"ffmpeg: {text}", flush=True)

    def close(self):
        self.dead = True
        try:
            self.inbox.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


class Processor:
    """
    Owns the ffmpeg instances, swaps in new ones when settings change or one
    fails, and serves aligned audio to the realtime callback.
    """

    def __init__(self, sample_rate, channels, cfg, latency_samples):
        self.sample_rate = sample_rate
        self.channels = channels
        self.capacity = 1 << int(np.ceil(np.log2(sample_rate * 2)))  # ~2s history
        self.cfg = {k: cfg[k] for k in CHAIN_KEYS}
        self.latency_samples = latency_samples
        self.max_offset = int(0.1 * sample_rate)
        self.fade_samples = int(SWAP_FADE_SECS * sample_rate)
        self.restarts = 0
        self.swaps = 0
        self._active = None
        self._warming = None
        self._retiring = None     # previous instance, still crossfading out
        self._fade_until = None   # absolute sample index where the fade ends
        self._pending_cfg = None
        self._apply_at = 0.0
        self._running = True
        self._fail_streak = 0
        self._fade_buf = None
        threading.Thread(target=self._supervise, daemon=True).start()

    # -- state ----------------------------------------------------------------

    @property
    def aligned(self):
        inst = self._active
        return bool(inst and inst.aligned)

    def offsets_ms(self):
        inst = self._active
        if not inst or not inst.aligned:
            return None
        return {v: round(o / self.sample_rate * 1000, 1) for v, o in inst.offsets.items()}

    def lag_samples(self, input_index):
        inst = self._active
        if not inst or inst.base is None:
            return None
        return input_index - min(t.end for t in inst.timelines.values())

    def set_config(self, cfg):
        """Ask for new filter settings; applied by warming up a new ffmpeg."""
        wanted = {k: cfg[k] for k in CHAIN_KEYS}
        if wanted == self.cfg:
            return
        self.cfg = wanted
        self._pending_cfg = wanted
        # Debounce, so dragging a knob doesn't spawn a process per step.
        self._apply_at = time.monotonic() + 0.4

    def close(self):
        self._running = False
        for name in ('_active', '_warming', '_retiring'):
            inst = getattr(self, name)
            setattr(self, name, None)
            if inst:
                inst.close()

    # -- realtime path ---------------------------------------------------------

    def feed(self, block, index):
        """Hand one input block to every live instance."""
        for inst in (self._active, self._warming):
            if inst is None or inst.dead:
                continue
            if inst.base is None:
                inst.begin(index)
            inst.feed(block)

    @staticmethod
    def _read_clamped(inst, variant, start, out):
        """
        Read this instance's output for dry samples [start, start+len(out)),
        never reaching back before its first live sample. The samples before
        `base` are the startup preroll (silence plus the alignment chirp) and
        must never reach the speakers; the engine fills that gap with dry audio.
        """
        n = out.shape[0]
        skip = max(0, inst.base - start)
        if skip >= n:
            return 0, 0
        view = out[skip:] if skip else out
        lo, hi = inst.timelines[variant].read_into(start + skip + inst.offsets[variant], view)
        return lo + skip, hi + skip

    def read_into(self, variant, start, out):
        """Processed audio for dry samples [start, start+len(out)). Empty until aligned."""
        inst = self._active
        if inst is None or not inst.aligned or inst.base is None:
            return 0, 0
        lo, hi = self._read_clamped(inst, variant, start, out)

        old = self._retiring
        if old is not None and old.aligned and hi > lo:
            # Crossfade from the instance being replaced so the swap is silent.
            fade_end = self._fade_until
            if fade_end is None or start >= fade_end:
                self._retiring, self._fade_until = None, None
                old.close()
            else:
                n = out.shape[0]
                if self._fade_buf is None or self._fade_buf.shape[0] != n:
                    self._fade_buf = np.zeros((n, self.channels), dtype=np.float32)
                prev = self._fade_buf
                prev.fill(0)
                plo, phi = self._read_clamped(old, variant, start, prev)
                if phi > plo:
                    idx = np.arange(start, start + n, dtype=np.float64)
                    ramp = np.clip((idx - (fade_end - self.fade_samples)) / self.fade_samples, 0.0, 1.0)
                    ramp = ramp.astype(np.float32)[:, None]
                    blend = slice(max(lo, plo), min(hi, phi))
                    out[blend] = prev[blend] + (out[blend] - prev[blend]) * ramp[blend]
        return lo, hi

    # -- alignment ---------------------------------------------------------------

    def _try_align(self, inst):
        """
        Locate the startup marker in each variant to learn how far that output
        stream is shifted. Runs once per ffmpeg start; until it succeeds the
        instance serves nothing (the engine plays dry audio instead).
        """
        marker = inst.marker
        m_len = marker.shape[0]
        span = self.max_offset
        pad = inst.preroll.shape[0] - m_len
        # An unshifted marker sits `pad` samples into the stream.
        probe_len = pad + m_len + span
        offsets = {}
        ref = marker[:, 0]
        probe = np.zeros((probe_len, self.channels), dtype=np.float32)
        for v, tl in inst.timelines.items():
            if tl.end < tl.start + probe_len:
                return  # not enough output yet
            probe.fill(0)
            lo, hi = tl.read_into(tl.start, probe)
            if hi - lo < probe_len:
                return
            x = probe[:, 0]
            n = 1 << int(np.ceil(np.log2(len(x) + m_len)))
            corr = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(ref, n)), n)[:len(x) - m_len + 1]
            peak = int(np.argmax(corr))
            if corr[peak] <= 5 * float(np.mean(np.abs(corr))):
                return  # marker not clearly found yet; try again shortly
            # Normalised match too: peak-versus-mean alone is weak on silence,
            # where a spurious peak would hand back a bogus offset.
            window = x[peak:peak + m_len]
            denom = float(np.linalg.norm(window) * np.linalg.norm(ref))
            if denom <= 0.0 or corr[peak] / denom < 0.3:
                return
            offset = peak - pad
            if abs(offset) > span:
                return  # implausible; wait for a cleaner read
            offsets[v] = offset

        inst.offsets = offsets
        inst.aligned = True
        print("DSP: aligned " + ", ".join(f"{v}={o:+d}" for v, o in offsets.items()) + " samples", flush=True)

    # -- supervisor --------------------------------------------------------------

    def _new_instance(self, cfg):
        return _Instance(self.sample_rate, self.channels, cfg, self.capacity)

    def _supervise(self):
        while self._running:
            try:
                self._tick()
            except Exception as e:
                print(f"DSP: supervisor error: {e}", flush=True)
            time.sleep(0.1)
        self.close()

    def _tick(self):
        active, warming = self._active, self._warming

        # 1. Nothing running, or the active one died: start fresh immediately.
        if active is None or active.dead:
            if active is not None:
                self._fail_streak += 1
                print("DSP: ffmpeg stopped unexpectedly, restarting", flush=True)
                self._active = None
                active.close()
            if self._fail_streak > 3:
                time.sleep(min(30, 2 ** self._fail_streak))
            if not self._running:
                return
            try:
                self._active = self._new_instance(self.cfg)
                self.restarts += 1
            except Exception as e:
                print(f"DSP: failed to start ffmpeg: {e}", flush=True)
                self._fail_streak += 1
            return

        # 2. A settings change is due: warm up a replacement alongside.
        if self._pending_cfg is not None and time.monotonic() >= self._apply_at and warming is None:
            cfg, self._pending_cfg = self._pending_cfg, None
            try:
                self._warming = self._new_instance(cfg)
                print("DSP: warming up new settings", flush=True)
            except Exception as e:
                print(f"DSP: failed to start replacement ffmpeg: {e}", flush=True)
            return

        # 3. Align whichever instances still need it.
        for inst in (active, warming):
            if inst is not None and not inst.dead and not inst.aligned and inst.base is not None \
                    and all(t.end > t.start for t in inst.timelines.values()):
                self._try_align(inst)

        if active.aligned:
            self._fail_streak = 0

        # 4. Promote the replacement once it is aligned and covers the playhead.
        if warming is not None:
            if warming.dead:
                self._warming = None
                warming.close()
                print("DSP: replacement ffmpeg failed, keeping current settings", flush=True)
            elif warming.aligned and warming.base is not None:
                covered = min(t.end for t in warming.timelines.values())
                playhead = min(t.end for t in active.timelines.values()) - self.latency_samples
                # Past the new instance's first live sample, or the swap would
                # serve its startup preroll.
                if covered >= playhead + self.fade_samples \
                        and playhead >= warming.base + self.fade_samples:
                    self._retiring = self._active
                    self._fade_until = playhead + self.fade_samples
                    self._active = warming
                    self._warming = None
                    self.swaps += 1
                    print("DSP: new settings live", flush=True)
