"""
Quick check of marker-based alignment: start the processor, feed a few seconds
of audio, and print the offsets it measures. Known-good values from the direct
cross-correlation probe: dc = -1024, dn = +176, both = +176 samples.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dsp import Processor, Timeline, VARIANTS  # noqa: E402

SR, FR = 48000, 1024
EXPECTED = {'dc': -1024, 'dn': 176, 'both': 176}
DELAY = int(0.25 * SR)

proc = Processor(SR, 2, sensitivity=5.0, strength=12.0)
dry = Timeline(1 << 17, 2, 0)
rng = np.random.default_rng(3)
index = 0
t0 = time.perf_counter()
out = np.zeros((FR, 2), dtype=np.float32)
dryblk = np.zeros((FR, 2), dtype=np.float32)
residuals = []

for i in range(int(8 * SR / FR)):
    d = t0 + i * FR / SR - time.perf_counter()
    if d > 0:
        time.sleep(d)
    t = (np.arange(FR) + i * FR) / SR
    sig = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.01 * rng.standard_normal(FR)
    block = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))
    dry.write(block)
    proc.feed(block, index)
    index += FR
    start = index - DELAY
    if start < 0 or not proc.aligned:
        continue
    # Compare the declicked variant against dry on clean audio: should be tiny.
    dryblk.fill(0)
    dry.read_into(start, dryblk)
    out.fill(0)
    lo, hi = proc.read_into('dc', start, out)
    if hi - lo == FR:
        residuals.append(float(np.abs(out - dryblk).mean()))

off = proc._inst.offsets if proc._inst else {}
print(f"measured offsets: {off}")
print(f"expected        : {EXPECTED}")
ok = all(off.get(v) == EXPECTED[v] for v in VARIANTS)
print(f"match: {'YES' if ok else 'NO'}")
if residuals:
    print(f"dc vs dry residual on clean audio: mean={np.mean(residuals):.5f} max={np.max(residuals):.5f} "
          f"(signal level ~0.16); {len(residuals)} blocks compared")
else:
    print("no aligned blocks compared")
proc.close()
