# Which PyAV step leaks? variant: frame | push | pushpull | full | nopts
import sys, av, numpy as np
from fractions import Fraction
def rss():
    for l in open('/proc/self/status'):
        if l.startswith('VmRSS'): return int(l.split()[1]) / 1024
variant, N, sr, fr = sys.argv[1], 40000, 44100, 512
g = av.filter.Graph()
src = g.add_abuffer(template=None, sample_rate=sr, format='fltp', layout='stereo', name='in')
f = g.add('afftdn', 'nr=12:nf=-50')
sink = g.add('abuffersink', name='out')
src.link_to(f); f.link_to(sink); g.configure()
x = np.ascontiguousarray((np.random.default_rng(0).standard_normal((2, fr)) * .1).astype(np.float32))
start = rss(); pts = 0; total_out = 0
for i in range(N):
    fm = av.AudioFrame.from_ndarray(x, format='fltp', layout='stereo')
    fm.sample_rate = sr
    if variant != 'nopts':
        fm.time_base = Fraction(1, sr); fm.pts = pts
    pts += fr
    if variant == 'frame': continue
    g.push(fm)
    if variant == 'push': continue
    while True:
        try:
            o = g.pull()
        except (av.error.EOFError, av.error.BlockingIOError):
            break
        if variant in ('full', 'nopts'):
            total_out += o.to_ndarray().shape[1]
        else:
            total_out += o.samples
print(f"{variant:9s} rss {start:.1f} -> {rss():.1f} MB over {N*fr/sr:.0f}s audio; in={pts} out={total_out} holdback={pts-total_out}", flush=True)
