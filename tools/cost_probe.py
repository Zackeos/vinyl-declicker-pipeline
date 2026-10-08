"""Per-feature CPU cost, to choose sensible defaults."""
import os, subprocess, threading, time
import numpy as np

SR, FR, SECS = 48000, 1024, 15.0
VARIANTS = ("pre", "dc", "dn", "both")
GENTLE = "adeclick=window=55:overlap=50:arorder=2:threshold=6.0:burst=2:method=a"
POPS = "adeclick=window=55:overlap=50:arorder=4:threshold=2.5:burst=8:method=a"
DECLIP = "adeclip=window=55:overlap=75:arorder=8:threshold=10:hsize=1000:method=add"
DENOISE = "afftdn=nr=12:nf=-50:tn=1"
PRE = ("[0:a]highpass=f=18:poles=2[r];[r]asplit=2[rm][rs];"
       "[rm]pan=mono|c0=0.5*c0+0.5*c1[mid];"
       "[rs]pan=mono|c0=0.5*c0-0.5*c1,highpass=f=150:poles=2[side];"
       "[mid][side]amerge=inputs=2,pan=stereo|c0=c0+c1|c1=c0-c1[pre0];")


def graph(pre, declip, two_stage):
    head = PRE if pre else "[0:a]anull[pre0];"
    chain = GENTLE if not two_stage else GENTLE + "," + POPS
    if declip:
        chain = DECLIP + "," + chain
    return (head + "[pre0]asplit=3[pre][a][b];"
            f"[a]{chain},asplit=2[dc][dc2];[dc2]{DENOISE}[both];[b]{DENOISE}[dn]")


def run(label, pre, declip, two_stage):
    rfds, wfds = {}, {}
    for v in VARIANTS:
        rfds[v], wfds[v] = os.pipe()
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer",
           "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
           "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0",
           "-filter_complex", graph(pre, declip, two_stage)]
    for v in VARIANTS:
        cmd += ["-map", f"[{v}]", "-f", "f32le", "-avioflags", "direct", "-flush_packets", "1", f"pipe:{wfds[v]}"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, pass_fds=tuple(wfds.values()), bufsize=0)
    for w in wfds.values():
        os.close(w)
    for v in VARIANTS:
        threading.Thread(target=lambda fd=rfds[v]: [None for _ in iter(lambda: os.read(fd, 65536), b"")], daemon=True).start()
    threading.Thread(target=lambda: [print("  ffmpeg: " + l.decode().strip(), flush=True) for l in p.stderr], daemon=True).start()
    rng = np.random.default_rng(0)
    t0 = time.perf_counter()
    for i in range(int(SECS * SR / FR)):
        d = t0 + i * FR / SR - time.perf_counter()
        if d > 0:
            time.sleep(d)
        t = (np.arange(FR) + i * FR) / SR
        sig = 0.25 * np.sin(2 * np.pi * 440 * t) + 0.01 * rng.standard_normal(FR)
        blk = np.ascontiguousarray(np.column_stack((sig, sig)).astype(np.float32))
        if p.poll() is not None:
            print("  exited early")
            break
        p.stdin.write(blk.tobytes())
    wall = time.perf_counter() - t0
    st = open("/proc/%d/stat" % p.pid).read().split()
    cpu = (int(st[13]) + int(st[14])) / os.sysconf("SC_CLK_TCK")
    print(f"{label:42s} {cpu / wall * 100:5.1f}% of one core", flush=True)
    try:
        p.stdin.close()
    except Exception:
        pass
    p.terminate()
    time.sleep(1)


run("baseline (1 declick + afftdn x2)", False, False, False)
run("+ pre-stage (rumble + bass mono)", True, False, False)
run("+ two-stage declick", True, False, True)
run("+ adeclip (everything)", True, True, True)
run("adeclip only (no two-stage)", True, True, False)
