"""
Offline test of the variable-rate playback maths, before it goes near speakers.

Drives the real Engine methods (_update_rate, _span, _read_dry, _resample) with
a constructed object: no audio device, no threads, no ffmpeg.

Checks: pitch accuracy, interpolation artifacts, glitches, delay trajectory,
and that the delay never goes negative or stalls short of its target.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settings  # noqa: E402
from audio_engine import Engine, CHANNELS  # noqa: E402
from dsp import Timeline  # noqa: E402

SR, FR = 48000, 1024
TONE = 1000.0
AMP = 0.5
MAX_STEP = AMP * 2 * np.pi * TONE / SR * 1.05   # steepest a clean tone can move, plus margin


def make_engine(start_delay_ms):
    opts = dict(settings.DEFAULTS)
    opts.update(sample_rate=SR)
    eng = Engine.__new__(Engine)          # no __init__: no threads, no device
    eng.opts = opts
    eng._bufs = {}
    eng._fallback = 0
    eng._proc = None
    eng._dry = Timeline(1 << 18, CHANNELS, 0)
    eng._index = 0
    eng._play = -SR * start_delay_ms / 1000.0
    eng._rate = 1.0
    eng._rate_target = 1.0
    eng._idle = True
    eng._latency = int(SR * opts['latency_ms'] / 1000)
    return eng


def run(seconds, idle_at, start_delay_ms):
    """idle_at(block) -> bool. Returns engine, played signal, delays(ms), rates, failures."""
    eng = make_engine(start_delay_ms)
    out = np.zeros((FR, CHANNELS), dtype=np.float32)
    played, delays, rates = [], [], []
    failures = 0
    for i in range(int(seconds * SR / FR)):
        t = (np.arange(FR) + i * FR) / SR
        sig = AMP * np.sin(2 * np.pi * TONE * t)
        eng._dry.write(np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32)))
        eng._index += FR
        eng._idle = idle_at(i)
        rate = eng._update_rate(eng._index, FR)
        if not eng._read_dry(out, FR, rate):
            # Before the playhead reaches zero there is simply no audio yet:
            # that is the stream's opening silence, not a fault.
            if eng._play >= 0:
                failures += 1
            out[:] = eng._buf('hold', FR)
        eng._buf('hold', FR)[:] = out
        eng._play += FR * rate
        played.append(out[:, 0].copy())
        delays.append((eng._index - eng._play) / SR * 1000)
        rates.append(rate)
    return eng, np.concatenate(played), np.array(delays), np.array(rates), failures


def tone_hz(x):
    w = np.hanning(len(x))
    mag = np.abs(np.fft.rfft(x * w))
    k = int(np.argmax(mag))
    if 0 < k < len(mag) - 1:
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        k = k + 0.5 * (a - c) / (a - 2 * b + c)
    return k * SR / len(x)


def thd(x):
    w = np.hanning(len(x))
    mag = np.abs(np.fft.rfft(x * w)) ** 2
    k = int(np.argmax(mag))
    fund = mag[max(0, k - 3):k + 4].sum()
    return 10 * np.log10((mag.sum() - fund) / (fund + 1e-20) + 1e-20)


def settle_time(delays, target, tol=2.0, after=0):
    hit = np.flatnonzero(np.abs(delays[after:] - target) <= tol)
    return None if hit.size == 0 else (hit[0] + after) * FR / SR


def report(name, sig, delays, rates, failures, target, switch_block):
    step = np.abs(np.diff(sig[int(0.5 * SR):]))
    worst = float(step.max())
    bad = int((step > MAX_STEP).sum())
    settled = settle_time(delays, target, after=switch_block)
    seg = sig[int((switch_block * FR / SR + 2) * SR):int((switch_block * FR / SR + 6) * SR)]
    cents = 1200 * np.log2(tone_hz(seg) / TONE) if seg.size > SR // 2 else float('nan')
    print(f"  {name}")
    print(f"    delay {delays[switch_block]:.0f} -> {delays[-1]:.1f} ms (target {target} ms), "
          f"min seen {delays.min():.1f} ms")
    print(f"    settled after {'%.1f s' % settled if settled is not None else 'NEVER'}")
    print(f"    rate during ramp {rates[switch_block + 100]:.5f}, pitch {cents:+.1f} cents, "
          f"artifacts {thd(seg):.1f} dB")
    print(f"    read failures {failures}, samples exceeding a clean tone slope: {bad} "
          f"(worst step {worst:.4f} vs {MAX_STEP:.4f})")
    return bad == 0 and failures == 0 and settled is not None and delays.min() > 0


print("=== engage: idle, then filters on (15 -> 200 ms) ===")
e, sig, d, r, f = run(40, lambda i: i < 100, start_delay_ms=15)
ok1 = report("engage", sig, d, r, f, e.opts['latency_ms'], 100)

print("\n=== release: filters on, then off (200 -> 15 ms) ===")
e2, sig2, d2, r2, f2 = run(80, lambda i: i > 500, start_delay_ms=200)
ok2 = report("release", sig2, d2, r2, f2, e2.opts['idle_latency_ms'], 500)

print("\n=== rapid toggling (switch flipped every 3 s) ===")
e3, sig3, d3, r3, f3 = run(40, lambda i: (i // 140) % 2 == 0, start_delay_ms=200)
step3 = np.abs(np.diff(sig3[int(0.5 * SR):]))
print(f"  min delay {d3.min():.1f} ms, max {d3.max():.1f} ms, read failures {f3}")
print(f"  samples exceeding a clean tone slope: {int((step3 > MAX_STEP).sum())} "
      f"(worst {step3.max():.4f} vs {MAX_STEP:.4f})")
ok3 = f3 == 0 and d3.min() > 0 and (step3 > MAX_STEP).sum() == 0

print("\nVERDICT:", "ALL CLEAN" if (ok1 and ok2 and ok3) else "PROBLEMS REMAIN")
