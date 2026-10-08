"""Is a 192k open really hitting the card, or is PipeWire resampling it?"""
import glob, time
import sounddevice as sd
import numpy as np

print("=== all audio devices seen by PortAudio ===")
for i, d in enumerate(sd.query_devices()):
    print("  %2d  in=%-3d out=%-3d sr=%-8s %s" %
          (i, d["max_input_channels"], d["max_output_channels"], int(d["default_samplerate"]), d["name"]))

print("\n=== ALSA cards ===")
print(open("/proc/asound/cards").read().rstrip())


def hw_params():
    out = []
    for p in sorted(glob.glob("/proc/asound/card*/pcm*/sub0/hw_params")):
        try:
            txt = open(p).read().strip()
        except Exception:
            continue
        if txt and txt != "closed":
            rate = [l for l in txt.splitlines() if l.startswith("rate:")]
            out.append("%s -> %s" % (p.split("/asound/")[1], rate[0] if rate else "open"))
    return out or ["(no pcm open)"]


def probe(label, dev, sr):
    seen = []
    def cb(indata, outdata, frames, t, status):
        seen.append(frames); outdata.fill(0)
    try:
        t0 = time.perf_counter()
        s = sd.Stream(device=(dev, dev), samplerate=sr, blocksize=1024,
                      channels=2, dtype="float32", callback=cb)
        s.start()
        open_ms = (time.perf_counter() - t0) * 1000
        time.sleep(1.2)
        hw = hw_params()
        t1 = time.perf_counter(); s.stop(); s.close()
        close_ms = (time.perf_counter() - t1) * 1000
        print("\n  %s (device=%s, asked %d Hz)" % (label, dev, sr))
        print("    open %.0f ms, close %.0f ms, callbacks=%d" % (open_ms, close_ms, len(seen)))
        for h in hw:
            print("    HARDWARE %s" % h)
    except Exception as e:
        print("\n  %s (device=%s, asked %d Hz)\n    FAILED: %s" %
              (label, dev, sr, str(e).strip().splitlines()[0][:80]))


hw_dev = next((i for i, d in enumerate(sd.query_devices())
               if "hw:1,0" in d["name"] and d["max_input_channels"] >= 2), None)
pw_dev = next((i for i, d in enumerate(sd.query_devices())
               if d["name"] == "pipewire"), None)
print("\nresolved: hw:1,0 index=%s   pipewire index=%s" % (hw_dev, pw_dev))
print("\n=== baseline (service running through PipeWire) ===")
for h in hw_params():
    print("    HARDWARE %s" % h)

if pw_dev is not None:
    probe("via PipeWire", pw_dev, 48000)
    probe("via PipeWire asking 192k", pw_dev, 192000)
if hw_dev is not None:
    probe("direct to card", hw_dev, 192000)
else:
    print("\n  hw:1,0 not exposed to PortAudio (card likely held by PipeWire)")
