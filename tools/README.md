# Hardware harnesses

These need a real sound card, a real FFmpeg, or both, so they are not part of
the pytest suite. They are the scripts that produced the measurements quoted in
the README and the commit history, kept so the numbers can be reproduced rather
than taken on trust.

Run them with the project venv, from the repository root:

```bash
./venv/bin/python tools/<name>.py
```

| Tool | What it checks |
| --- | --- |
| `dsp_verify.py` | End to end: alignment, lag, recovery from a killed FFmpeg and from a settings change, click removal, CPU and memory over a few minutes. |
| `align_check.py` | Marker alignment against known per-variant offsets, and that a declicked variant is bit-identical to dry audio on clean input. |
| `marker_leak_test.py` | That the alignment chirp never reaches the output. Correlates everything played against the marker across swaps and a cold restart. |
| `varispeed_test.py` | Variable-rate playback: pitch accuracy, interpolation artifacts, glitch detection and the delay trajectory in both directions. |
| `hotswap_test.py` | That changing settings mid-stream costs no unprocessed audio, unlike a cold restart. |
| `offset_probe.py` | Measures each variant's offset directly by cross-correlation, independently of the marker scheme. |
| `denoise_probe.py` | Whether `afftdn` actually removes anything, across noise floors and `track_noise` settings. |
| `content_probe.py` | Declick cost on music versus a quiet groove: the cost is content-dependent, not just settings-dependent. |
| `cost_probe.py` | CPU cost of each chain option, for choosing defaults. |
| `chain_probe.py` | That the four-variant graph builds, all outputs flow, and the pre-stage is transparent. |
| `cpu_rate.py` | CPU and lag at 48, 96 and 192 kHz. |
| `cb_cpu.py` | Cost of a bare duplex stream with no processing, as a baseline. |
| `rate_probe2.py` | Whether a requested sample rate reaches the hardware or is resampled by the sound server. |
| `leak_isolate.py` | The PyAV filter-graph leak that led to running FFmpeg out of process. |
| `pipe_test.py` | The FFmpeg-subprocess prototype that replaced it: latency, CPU and memory. |
| `generate_test_audio.py` | Writes a stereo test file used by some of the probes. |
| `test_serial.py` | Prints raw lines from the front panel, for checking wiring and the sketch. |

`leak_isolate.py` and `pipe_test.py` are kept deliberately: they are the
measurements behind the main architectural decision, and they still run.
