"""Does afftdn actually remove anything? Compare the pre and dn variants."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings
from dsp import Processor

SR, FR, SECS = 48000, 1024, 12.0
LAT = int(0.3 * SR)


def run(label, nf, tn, nr):
    cfg = settings.DEFAULTS.copy()
    cfg.update({"noise_floor": nf, "track_noise": tn, "strength": nr})
    proc = Processor(SR, 2, cfg, LAT)
    rng = np.random.default_rng(7)
    pre_buf = np.zeros((FR, 2), dtype=np.float32)
    dn_buf = np.zeros((FR, 2), dtype=np.float32)
    index = 0
    prog_e, rem_e, n = 0.0, 0.0, 0
    t0 = time.perf_counter()
    for i in range(int(SECS * SR / FR)):
        d = t0 + i * FR / SR - time.perf_counter()
        if d > 0:
            time.sleep(d)
        t = (np.arange(FR) + i * FR) / SR
        music = 0.25 * np.sin(2 * np.pi * 220 * t) + 0.15 * np.sin(2 * np.pi * 1320 * t)
        hiss = 0.0018 * rng.standard_normal(FR)          # about -55 dBFS surface noise
        sig = music + hiss
        if i % 12 == 5:
            sig[300:306] += 0.4
        blk = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))
        proc.feed(blk, index)
        index += FR
        start = index - LAT
        pre_buf.fill(0); dn_buf.fill(0)
        a = proc.read_into("pre", start, pre_buf)
        b = proc.read_into("dn", start, dn_buf)
        if i > 120 and a[1] - a[0] == FR and b[1] - b[0] == FR:
            diff = pre_buf - dn_buf
            prog_e += float(np.mean(pre_buf ** 2))
            rem_e += float(np.mean(diff ** 2))
            n += 1
    if n:
        prog_db = 10 * np.log10(prog_e / n + 1e-20)
        rem_db = 10 * np.log10(rem_e / n + 1e-20)
        print("%-40s programme %6.1f dB   removed %7.1f dB   (%+.1f dB relative)" %
              (label, prog_db, rem_db, rem_db - prog_db), flush=True)
    else:
        print("%-40s no aligned blocks" % label, flush=True)
    proc.close()
    time.sleep(1)


run("nf=-50 tn=1 nr=12 (current default)", -50, True, 12)
run("nf=-50 tn=0 nr=12", -50, False, 12)
run("nf=-30 tn=0 nr=12", -30, False, 12)
run("nf=-55 tn=0 nr=24", -55, False, 24)
run("nf=-70 tn=1 nr=12", -70, True, 12)
