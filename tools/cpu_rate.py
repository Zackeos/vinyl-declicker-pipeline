"""
How much CPU does the processing chain need at 48 / 96 / 192 kHz?

Feeds realtime-paced audio through dsp.Processor at each rate and measures the
ffmpeg process CPU, the Python feed/read CPU, and the resulting lag. Run with
the live service stopped for clean numbers, or accept some contention.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dsp import Processor, Timeline  # noqa: E402

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0


def proc_cpu(pid):
    st = open(f'/proc/{pid}/stat').read().split()
    return (int(st[13]) + int(st[14])) / os.sysconf('SC_CLK_TCK')


for sr in (48000, 96000, 192000):
    fr = 1024 * sr // 48000          # same ~21 ms block duration at every rate
    lat = int(0.3 * sr)
    proc = Processor(sr, 2, sensitivity=5.0, strength=12.0)
    dry = Timeline(1 << int(np.ceil(np.log2(sr * 2))), 2, 0)
    out = np.zeros((fr, 2), dtype=np.float32)
    rng = np.random.default_rng(0)
    index = 0
    lags, late = [], 0
    t0 = time.perf_counter()
    py0 = sum(os.times()[:2])
    ff_pid, ff0 = None, 0.0

    for i in range(int(SECS * sr / fr)):
        target = t0 + i * fr / sr
        d = target - time.perf_counter()
        if d > 0:
            time.sleep(d)
        elif d < -0.05:
            late += 1               # we could not keep up with realtime
        t = (np.arange(fr) + i * fr) / sr
        sig = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.01 * rng.standard_normal(fr)
        block = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))
        dry.write(block)
        proc.feed(block, index)
        index += fr
        proc.read_into('both', index - lat, out)
        if ff_pid is None and proc._inst is not None:
            ff_pid = proc._inst.proc.pid
            ff0 = proc_cpu(ff_pid)
        lag = proc.lag_samples(index)
        if lag is not None and i > 100:
            lags.append(lag / sr * 1000)

    wall = time.perf_counter() - t0
    py = sum(os.times()[:2]) - py0
    ff = proc_cpu(proc._inst.proc.pid) - ff0 if proc._inst else float('nan')
    print(f"{sr/1000:5.1f} kHz block={fr:5d}  ffmpeg={ff/wall*100:5.1f}%  python={py/wall*100:5.1f}%  "
          f"total={(ff+py)/wall*100:5.1f}% of one core   lag avg/max={np.mean(lags):5.0f}/{np.max(lags):5.0f} ms  "
          f"aligned={proc.aligned}  late_blocks={late}", flush=True)
    proc.close()
    time.sleep(1)
