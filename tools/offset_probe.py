"""
Measure the true sample offset of each ffmpeg variant relative to the dry input.

Feeds a chirp (which neither filter should remove) through dsp.Processor,
collects each variant's output stream, and cross-correlates against the dry
signal to find the offset that lines them up.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dsp import Processor, VARIANTS  # noqa: E402

SR, FR = 48000, 1024
SECS = 12.0
DELAY = int(SR * 0.25)

proc = Processor(SR, 2, sensitivity=5.0, strength=12.0)
time.sleep(1.0)

blocks = int(SECS * SR / FR)
total = blocks * FR
t = np.arange(total) / SR
# 200 Hz -> 3 kHz sweep, repeating every 2 s, plus a little noise
sweep = 0.3 * np.sin(2 * np.pi * (200 * (t % 2) + (3000 - 200) / 2 / 2 * (t % 2) ** 2))
sweep += 0.004 * np.random.default_rng(0).standard_normal(total)
dry_full = np.ascontiguousarray(np.column_stack((sweep, sweep)).astype(np.float32))

collected = {v: np.full(total, np.nan, dtype=np.float32) for v in VARIANTS}
buf = np.zeros((FR, 2), dtype=np.float32)
index = 0
t0 = time.perf_counter()
for i in range(blocks):
    d = t0 + i * FR / SR - time.perf_counter()
    if d > 0:
        time.sleep(d)
    block = dry_full[i * FR:(i + 1) * FR]
    proc.feed(block, index)
    index += FR
    start = index - DELAY
    if start < 0:
        continue
    for v in VARIANTS:
        buf.fill(np.nan)
        lo, hi = proc.read_into(v, start, buf)
        if hi > lo:
            seg = slice(start + lo, start + hi)
            collected[v][seg] = buf[lo:hi, 0]

print(f"fed {total} samples at {SR} Hz\n")
mid = slice(int(4 * SR), int(8 * SR))
ref = dry_full[mid, 0]
for v in VARIANTS:
    got = collected[v][mid]
    missing = int(np.isnan(got).sum())
    got = np.nan_to_num(got)
    # search +-0.75 s for the offset that best matches the dry signal
    span = int(0.75 * SR)
    corr = np.correlate(np.nan_to_num(collected[v][int(4 * SR) - span:int(8 * SR) + span]), ref, mode='valid')
    best = int(np.argmax(corr)) - span
    aligned = np.nan_to_num(collected[v][int(4 * SR) + best:int(8 * SR) + best])
    resid = float(np.abs(aligned - ref).mean()) if aligned.shape == ref.shape else float('nan')
    print(f"{v:5s} offset = {best:+6d} samples ({best / SR * 1000:+7.1f} ms)   "
          f"missing={missing:6d}  residual after aligning = {resid:.4f}")
    print(f"        residual with NO offset = {float(np.abs(got - ref).mean()):.4f}")
proc.close()
