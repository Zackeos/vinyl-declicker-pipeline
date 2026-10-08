# CPU cost of a bare sounddevice duplex stream (silent output) - no DSP.
import sys, time, os, numpy as np, sounddevice as sd
rate, bs, secs = int(sys.argv[1]), int(sys.argv[2]), 15
sizes = []
def cb(indata, outdata, frames, t, status):
    sizes.append(frames)
    outdata.fill(0)
def cpu(): 
    t = os.times(); return t.user + t.system
dev = [i for i, d in enumerate(sd.query_devices()) if d['name'] == 'pipewire'][0]
with sd.Stream(device=(dev, dev), samplerate=rate, blocksize=bs, channels=2, dtype='float32', callback=cb):
    time.sleep(1); c0 = cpu(); n0 = len(sizes)
    time.sleep(secs)
    c = cpu() - c0
sz = sizes[n0:]
print(f"rate={rate} blocksize={bs}: cpu={c/secs*100:5.1f}%  callbacks/s={len(sz)/secs:6.1f}  frames min/max={min(sz)}/{max(sz)}", flush=True)
