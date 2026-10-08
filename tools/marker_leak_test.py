"""
Can the alignment chirp ever reach the speakers?

Feeds a steady tone, forces several settings swaps and a cold restart, and
correlates everything the engine would have played against the chirp. A clean
run shows the chirp nowhere above the noise; before the fix it appears right
after each swap and restart.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings  # noqa: E402
from dsp import Processor, Timeline, build_marker  # noqa: E402

SR, FR = 48000, 1024
LAT = int(0.3 * SR)
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0

cfg = settings.DEFAULTS.copy()
proc = Processor(SR, 2, cfg, LAT)
dry = Timeline(1 << 17, 2, 0)
out = np.zeros((FR, 2), dtype=np.float32)
dryblk = np.zeros((FR, 2), dtype=np.float32)
_, marker = build_marker(SR, 2)
ref = marker[:, 0].astype(np.float64)

played = []
events = []
index = 0
rng = np.random.default_rng(11)
t0 = time.perf_counter()
blocks = int(SECS * SR / FR)
done = set()

for i in range(blocks):
    d = t0 + i * FR / SR - time.perf_counter()
    if d > 0:
        time.sleep(d)
    t = (np.arange(FR) + i * FR) / SR
    sig = 0.2 * np.sin(2 * np.pi * 330 * t) + 0.002 * rng.standard_normal(FR)
    block = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))

    dry.write(block)
    proc.feed(block, index)
    index += FR

    start = index - LAT
    dryblk.fill(0)
    dry.read_into(start, dryblk)
    out.fill(0)
    lo, hi = proc.read_into('both', start, out)
    if lo > 0:
        out[:lo] = dryblk[:lo]
    if hi < FR:
        out[hi:] = dryblk[hi:]
    played.append(out[:, 0].copy())

    secs = i * FR / SR
    for at, action in ((10, 'swap: sensitivity 7'), (20, 'swap: rumble off'),
                       (30, 'swap: strength 20'), (40, 'kill ffmpeg')):
        if secs >= at and at not in done:
            done.add(at)
            if action.startswith('swap'):
                key, value = {10: ('sensitivity', 7.0), 20: ('rumble', False),
                              30: ('strength', 20.0)}[at]
                cfg[key] = value
                proc.set_config(cfg)
            else:
                pid = proc._active.proc.pid if proc._active else None
                if pid:
                    os.kill(pid, 9)
            events.append((at, action))

signal = np.concatenate(played).astype(np.float64)
# Normalised cross-correlation of the played audio against the chirp
m = len(ref)
n = 1 << int(np.ceil(np.log2(len(signal) + m)))
corr = np.fft.irfft(np.fft.rfft(signal, n) * np.conj(np.fft.rfft(ref, n)), n)[:len(signal) - m + 1]
energy = np.sqrt(np.convolve(signal ** 2, np.ones(m), 'valid'))
norm = corr / (energy * np.sqrt(np.sum(ref ** 2)) + 1e-12)

peak = float(np.max(np.abs(norm)))
where = float(np.argmax(np.abs(norm))) / SR
print(f"events: {events}")
print(f"swaps={proc.swaps} restarts={proc.restarts}")
print(f"best chirp match in played audio: {peak:.3f} at t={where:.1f}s")
print("A clean run stays well below 0.30; a leaked chirp correlates above 0.7.")
print("VERDICT:", "CLEAN" if peak < 0.30 else "CHIRP AUDIBLE IN OUTPUT")
proc.close()
