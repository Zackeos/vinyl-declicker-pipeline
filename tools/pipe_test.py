# (a) does rebuilding the PyAV graph release leaked memory?  (b) ffmpeg subprocess pipe: holdback, cpu, rss.
import sys, os, time, threading, subprocess, av, numpy as np
def rss(pid='self'):
    for l in open(f'/proc/{pid}/status'):
        if l.startswith('VmRSS'): return int(l.split()[1]) / 1024
sr, fr = 48000, 1024
x = np.ascontiguousarray((np.random.default_rng(0).standard_normal((2, fr)) * .1).astype(np.float32))
if sys.argv[1] == 'rebuild':
    def build():
        g = av.filter.Graph(); s = g.add_abuffer(template=None, sample_rate=sr, format='fltp', layout='stereo', name='in')
        f = g.add('anull'); k = g.add('abuffersink', name='out'); s.link_to(f); f.link_to(k); g.configure(); return g
    g = build(); r0 = rss()
    for i in range(28125 * 2):  # 1200s
        fm = av.AudioFrame.from_ndarray(x, format='fltp', layout='stereo'); fm.sample_rate = sr; g.push(fm)
        while True:
            try: g.pull().to_ndarray()
            except (av.error.EOFError, av.error.BlockingIOError): break
        if i % 2812 == 0 and i: g = None; g = build()   # every 60s
        if i % 14062 == 0: print(f"  rebuild t={i*fr/sr:5.0f}s rss={rss():.1f}", flush=True)
    print(f"rebuild-every-60s: rss {r0:.1f}->{rss():.1f}MB over 1200s")
else:
    D = "adeclick=window=55:overlap=50:arorder=2:threshold=4.50:burst=2:method=a,afftdn=nr=12:nf=-50"
    p = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-fflags', 'nobuffer', '-probesize', '32', '-analyzeduration', '0',
                          '-f', 'f32le', '-ar', str(sr), '-ac', '2', '-i', 'pipe:0', '-af', D,
                          '-f', 'f32le', '-flush_packets', '1', 'pipe:1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    got = [0]
    def reader():
        while True:
            b = p.stdout.read(8192)
            if not b: break
            got[0] += len(b) // 8
    threading.Thread(target=reader, daemon=True).start()
    inter = x.T.copy().tobytes()
    sent = 0; hold = []; t0 = time.perf_counter(); c0 = sum(os.times()[:2])
    secs = 120
    for i in range(int(secs * sr / fr)):
        target = t0 + i * fr / sr
        d = target - time.perf_counter()
        if d > 0: time.sleep(d)
        p.stdin.write(inter); sent += fr
        if i > 200: hold.append(sent - got[0])
    pc = open(f'/proc/{p.pid}/stat').read().split(); tck = os.sysconf('SC_CLK_TCK')
    ff_cpu = (int(pc[13]) + int(pc[14])) / tck
    print(f"pipe: holdback min/avg/max = {min(hold)}/{sum(hold)//len(hold)}/{max(hold)} samples "
          f"({max(hold)/sr*1000:.0f}ms max); ffmpeg cpu={ff_cpu/secs*100:.1f}% py cpu={(sum(os.times()[:2])-c0)/secs*100:.1f}% ffmpeg rss={rss(p.pid):.1f}MB")
    p.stdin.close(); p.wait()
