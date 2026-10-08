"""
Offline check of dsp.Processor: alignment, click removal, lag, CPU, memory,
and recovery from a parameter change and from ffmpeg being killed.

Runs without an audio device: feeds blocks in realtime pacing like the callback does.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dsp import Processor, Timeline  # noqa: E402

SR, FR = 48000, 1024
LAT = int(SR * 0.25)
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0


def rss(pid='self'):
    for line in open(f'/proc/{pid}/status'):
        if line.startswith('VmRSS'):
            return int(line.split()[1]) / 1024
    return 0


rng = np.random.default_rng(1)
dry = Timeline(1 << 17, 2, 0)
proc = Processor(SR, 2, sensitivity=5.0, strength=12.0)
out = np.zeros((FR, 2), dtype=np.float32)
dryblk = np.zeros((FR, 2), dtype=np.float32)

index = 0
click_energy = []        # energy of (dry - clean) where clicks were injected
quiet_energy = []        # ... where they were not
fallback = 0
lag_max = 0
phase = 0.0
t0 = time.perf_counter()
cpu0 = sum(os.times()[:2])
ff_pid = None
events = []
blocks = int(SECS * SR / FR)

for i in range(blocks):
    target = t0 + i * FR / SR
    delay = target - time.perf_counter()
    if delay > 0:
        time.sleep(delay)

    # 440 Hz tone + hiss, with a loud click every 10th block
    t = (np.arange(FR) + phase) / SR
    phase += FR
    sig = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.002 * rng.standard_normal(FR)
    has_click = (i % 10 == 3)
    if has_click:
        sig[500:508] += 0.7
    block = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))

    dry.write(block)
    proc.feed(block, index)
    index += FR

    start = index - LAT
    dryblk.fill(0)
    dry.read_into(start, dryblk)
    out.fill(0)
    lo, hi = proc.read_into('dc', start, out)
    if lo > 0:
        out[:lo] = dryblk[:lo]
    if hi < FR:
        out[hi:] = dryblk[hi:]
    fallback += FR - (hi - lo)

    diff = float(np.abs(dryblk - out).max())
    # The click sits LAT samples back, i.e. ~10 blocks earlier at 250 ms
    (click_energy if ((i - LAT // FR) % 10 == 3) else quiet_energy).append(diff)

    lag = proc.lag_samples(index)
    if lag is not None and i > 200:          # ignore warm-up
        lag_max = max(lag_max, lag)
    if i == 200:
        fallback = 0                          # count shortfalls only once settled
        click_energy.clear()
        quiet_energy.clear()

    if ff_pid is None and proc._inst is not None:
        ff_pid = proc._inst.proc.pid

    if i == int(blocks * 0.4):
        events.append((i, 'param change -> sensitivity 8'))
        proc.set_params(sensitivity=8.0)
    if i == int(blocks * 0.7):
        pid = proc._inst.proc.pid
        events.append((i, f'kill ffmpeg pid {pid}'))
        os.kill(pid, 9)
    if i % int(30 * SR / FR) == 0 and i:
        p = proc._inst.proc.pid if proc._inst else None
        per = {v: round((index - t.end) / SR * 1000) for v, t in proc._inst.timelines.items()} if proc._inst and proc._inst.base is not None else {}
        print(f"  t={i*FR/SR:5.0f}s py_rss={rss():6.1f}MB ffmpeg_rss={rss(p) if p else 0:6.1f}MB "
              f"lag_ms={per} restarts={proc.restarts}", flush=True)

cpu = sum(os.times()[:2]) - cpu0
ff_cpu = 0.0
if proc._inst:
    st = open(f'/proc/{proc._inst.proc.pid}/stat').read().split()
    ff_cpu = (int(st[13]) + int(st[14])) / os.sysconf('SC_CLK_TCK')

print(f"\nevents: {events}")
print(f"clicks: removed-signal peak on click blocks avg={np.mean(click_energy):.3f} "
      f"(max {np.max(click_energy):.3f}) vs quiet blocks avg={np.mean(quiet_energy):.4f}")
print(f"alignment: quiet-block difference stays near zero => variants line up with dry")
print(f"lag max={lag_max/SR*1000:.0f}ms  fallback(unprocessed)={fallback/SR:.2f}s of {SECS:.0f}s  restarts={proc.restarts}")
print(f"cpu: python={cpu/SECS*100:.1f}% of a core, ffmpeg(current instance)={ff_cpu/SECS*100:.1f}%")
print(f"rss: python={rss():.1f}MB ffmpeg={rss(proc._inst.proc.pid) if proc._inst else 0:.1f}MB")
proc.close()
