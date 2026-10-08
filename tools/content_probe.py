"""Is the declick cost content-dependent? Quiet groove vs music, two-stage on/off."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings
from dsp import Processor

SR, FR, SECS = 48000, 1024, 20.0
LAT = int(0.3 * SR)


def cpu_of(pid):
    st = open("/proc/%d/stat" % pid).read().split()
    return (int(st[13]) + int(st[14])) / os.sysconf("SC_CLK_TCK")


def run(label, two_stage, quiet):
    cfg = settings.DEFAULTS.copy()
    cfg["two_stage"] = two_stage
    proc = Processor(SR, 2, cfg, LAT)
    rng = np.random.default_rng(2)
    out = np.zeros((FR, 2), dtype=np.float32)
    index, pid, c0 = 0, None, 0.0
    lags = []
    t0 = time.perf_counter()
    for i in range(int(SECS * SR / FR)):
        d = t0 + i * FR / SR - time.perf_counter()
        if d > 0:
            time.sleep(d)
        t = (np.arange(FR) + i * FR) / SR
        if quiet:
            sig = 0.0004 * rng.standard_normal(FR)          # idle groove, about -70 dBFS
        else:
            sig = (0.22 * np.sin(2 * np.pi * 220 * t) + 0.12 * np.sin(2 * np.pi * 1320 * t)
                   + 0.004 * rng.standard_normal(FR))        # music-ish
        if i % 12 == 5:
            sig[300:306] += 0.5
        blk = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))
        proc.feed(blk, index)
        index += FR
        proc.read_into("both", index - LAT, out)
        if pid is None and proc._active is not None:
            pid = proc._active.proc.pid
            c0 = cpu_of(pid)
        lag = proc.lag_samples(index)
        if lag is not None and i > 100:
            lags.append(lag / SR * 1000)
    wall = time.perf_counter() - t0
    used = cpu_of(pid) - c0 if pid else float("nan")
    print("%-34s ffmpeg %5.1f%% of one core   lag avg/max %4.0f/%4.0f ms" %
          (label, used / wall * 100, np.mean(lags), np.max(lags)), flush=True)
    proc.close()
    time.sleep(1)


run("music, single-stage", False, False)
run("music, two-stage", True, False)
run("quiet groove, single-stage", False, True)
run("quiet groove, two-stage", True, True)
