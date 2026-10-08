"""
Offline test of the v2 processor: does changing settings while running cause
any gap, misalignment or click?

Timeline of the run:
    t=15s  toggle two-stage declicking  -> hot swap
    t=30s  change sensitivity           -> hot swap
    t=45s  kill ffmpeg                  -> cold restart (dry audio meanwhile)

Reports unprocessed (dry fallback) time per phase, alignment after each swap,
and the largest sample-to-sample jump in the output, which is what a click at a
swap boundary would look like.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings  # noqa: E402
from dsp import Processor, Timeline  # noqa: E402

SR, FR = 48000, 1024
LAT = int(0.3 * SR)
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

cfg = settings.DEFAULTS.copy()
proc = Processor(SR, 2, cfg, LAT)
dry = Timeline(1 << 17, 2, 0)
out = np.zeros((FR, 2), dtype=np.float32)
dryblk = np.zeros((FR, 2), dtype=np.float32)

rng = np.random.default_rng(5)
index = 0
phase_names = {0: 'startup', 1: 'after two_stage swap', 2: 'after sensitivity swap', 3: 'after ffmpeg kill'}
fallback = {k: 0 for k in phase_names}
samples = {k: 0 for k in phase_names}
phase = 0
worst_jump = 0.0
worst_at = None
tail = np.zeros(2, dtype=np.float32)
events = []
blocks = int(SECS * SR / FR)
t0 = time.perf_counter()

for i in range(blocks):
    d = t0 + i * FR / SR - time.perf_counter()
    if d > 0:
        time.sleep(d)
    t = (np.arange(FR) + i * FR) / SR
    sig = 0.25 * np.sin(2 * np.pi * 220 * t) + 0.01 * rng.standard_normal(FR)
    if i % 12 == 5:
        sig[300:306] += 0.6                      # a click to remove
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

    fallback[phase] += FR - (hi - lo)
    samples[phase] += FR

    # largest step between consecutive output samples (clicks show up here)
    if i > 60:
        joined = np.vstack((tail[None, :], out))
        jump = float(np.abs(np.diff(joined[:, 0])).max())
        if jump > worst_jump:
            worst_jump, worst_at = jump, round(i * FR / SR, 1)
    tail = out[-1].copy()

    secs = i * FR / SR
    if phase == 0 and secs >= 15:
        phase = 1
        cfg['two_stage'] = not cfg['two_stage']
        proc.set_config(cfg)
        events.append((15, f"two_stage -> {cfg['two_stage']}"))
    elif phase == 1 and secs >= 30:
        phase = 2
        cfg['sensitivity'] = 7.5
        proc.set_config(cfg)
        events.append((30, "sensitivity -> 7.5"))
    elif phase == 2 and secs >= 45:
        phase = 3
        pid = proc._active.proc.pid if proc._active else None
        if pid:
            os.kill(pid, 9)
        events.append((45, f"killed ffmpeg {pid}"))

print("\nevents:", events)
for k in sorted(phase_names):
    if samples[k]:
        print(f"  {phase_names[k]:24s} unprocessed {fallback[k] / SR * 1000:7.0f} ms "
              f"of {samples[k] / SR:5.1f} s")
print(f"swaps={proc.swaps} restarts={proc.restarts} aligned={proc.aligned} offsets={proc.offsets_ms()}")
print(f"largest sample-to-sample jump in output: {worst_jump:.4f} at t={worst_at}s "
      f"(a 220 Hz tone at 0.25 steps about {0.25 * 2 * np.pi * 220 / SR:.4f} per sample)")
proc.close()
