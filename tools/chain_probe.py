"""Validate the v2 filter chain: 4 variants, pre-stage transparency, CPU cost."""
import os, subprocess, threading, time
import numpy as np

SR, FR, SECS = 48000, 1024, 25.0
VARIANTS = ("pre", "dc", "dn", "both")

RUMBLE = "highpass=f=18:poles=2"
BASS_MONO_HZ = 150
DECLIP = "adeclip=window=55:overlap=75:arorder=8:threshold=10:hsize=1000:method=add"
GENTLE = "adeclick=window=55:overlap=50:arorder=2:threshold=6.0:burst=2:method=a"
POPS = "adeclick=window=55:overlap=50:arorder=4:threshold=2.5:burst=8:method=a"
DENOISE = "afftdn=nr=12:nf=-50:tn=1"


def build_graph(full):
    if full:
        pre = (f"[0:a]{RUMBLE}[r];"
               f"[r]asplit=2[rm][rs];"
               f"[rm]pan=mono|c0=0.5*c0+0.5*c1[mid];"
               f"[rs]pan=mono|c0=0.5*c0-0.5*c1,highpass=f={BASS_MONO_HZ}:poles=2[side];"
               f"[mid][side]amerge=inputs=2,pan=stereo|c0=c0+c1|c1=c0-c1[pre0];")
        declick = f"{DECLIP},{GENTLE},{POPS}"
    else:
        pre = "[0:a]anull[pre0];"
        declick = GENTLE
    return (pre +
            "[pre0]asplit=3[pre][a][b];"
            f"[a]{declick},asplit=2[dc][dc2];"
            f"[dc2]{DENOISE}[both];"
            f"[b]{DENOISE}[dn]")


def run(label, full):
    read_fds, write_fds = {}, {}
    for v in VARIANTS:
        read_fds[v], write_fds[v] = os.pipe()
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32",
           "-analyzeduration", "0", "-f", "f32le", "-ar", str(SR), "-ac", "2",
           "-i", "pipe:0", "-filter_complex", build_graph(full)]
    for v in VARIANTS:
        cmd += ["-map", f"[{v}]", "-f", "f32le", "-avioflags", "direct",
                "-flush_packets", "1", f"pipe:{write_fds[v]}"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, pass_fds=tuple(write_fds.values()), bufsize=0)
    for w in write_fds.values():
        os.close(w)
    got = {v: [] for v in VARIANTS}

    def reader(v, fd):
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            got[v].append(np.frombuffer(b, dtype=np.float32))
    for v in VARIANTS:
        threading.Thread(target=reader, args=(v, read_fds[v]), daemon=True).start()
    threading.Thread(target=lambda: [print("  ffmpeg: " + l.decode().strip(), flush=True)
                                     for l in p.stderr], daemon=True).start()

    rng = np.random.default_rng(0)
    fed, sent = [], 0
    t0 = time.perf_counter()
    for i in range(int(SECS * SR / FR)):
        d = t0 + i * FR / SR - time.perf_counter()
        if d > 0:
            time.sleep(d)
        t = (np.arange(FR) + i * FR) / SR
        left = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.01 * rng.standard_normal(FR)
        right = 0.25 * np.sin(2 * np.pi * 660 * t) + 0.01 * rng.standard_normal(FR)
        if i % 10 == 3:
            left[500:508] += 0.7
        block = np.ascontiguousarray(np.column_stack((left, right)).astype(np.float32))
        fed.append(block)
        if p.poll() is not None:
            print("  ffmpeg exited early")
            break
        p.stdin.write(block.tobytes())
        sent += FR
    wall = time.perf_counter() - t0
    st = open("/proc/%d/stat" % p.pid).read().split()
    cpu = (int(st[13]) + int(st[14])) / os.sysconf("SC_CLK_TCK")
    time.sleep(0.5)
    print(f"{label}: ffmpeg cpu = {cpu / wall * 100:.1f}% of one core")
    for v in VARIANTS:
        n = sum(len(c) for c in got[v]) // 2
        print(f"    {v:5s} {n:8d} samples, {n - sent:+6d} vs input")
    dry = np.concatenate(fed)[:, 0]
    pre = np.concatenate(got["pre"])[0::2] if got["pre"] else np.zeros(0)
    lo, hi, span = int(5 * SR), int(15 * SR), 4000
    if pre.size > hi + span:
        corr = np.correlate(pre[lo - span:hi + span], dry[lo:hi], mode="valid")
        off = int(np.argmax(corr)) - span
        resid = np.abs(pre[lo + off:hi + off] - dry[lo:hi]).mean()
        print(f"    pre offset {off:+d} samples, residual vs dry {resid:.5f} (level {np.abs(dry[lo:hi]).mean():.3f})")
    try:
        p.stdin.close()
    except Exception:
        pass
    p.terminate()
    time.sleep(1)

run("current-equivalent (1 declick + afftdn)", False)
run("full v2 (rumble+bassmono+adeclip+2-stage+afftdn tn)", True)
