# Vinyl Processor

A real-time declicker and denoiser that sits between a turntable and an
amplifier. Audio comes in from a USB interface, is processed continuously, and
goes back out to the speakers. Three switches on a front panel decide what you
hear; everything else is configured from a web page.

The declicking and denoising themselves are FFmpeg's `adeclick`, `adeclip` and
`afftdn` filters. This project is what goes around them: the real-time pipeline
that keeps a live stream flowing through an out-of-process FFmpeg, the
sample-exact alignment that makes its outputs usable, the control surface, the
automatic tuning and the hardware integration.

<!-- PHOTO: the box in the rack, front panel switches visible -->
<!-- PHOTO: the web UI on a phone, with a record playing -->
<!-- AUDIO: before / after clip of the same passage, declicker on and off -->

## The hardware chain

```
  turntable -> phono stage -> USB interface -> [ this machine ] -> amplifier -> speakers
                                                     ^
                                            Arduino front panel
                                             (3 toggle switches)
```

Built against a Technics SL-1500C, a Behringer UMC202HD and an Arduino Leonardo,
on a small Linux box running PipeWire. None of that is required: devices are
chosen by name, the panel is optional, and so is the sound server.

## Signal flow

Input is written to a ring buffer and fed to one long-running FFmpeg, which
renders four versions of the signal at once. Playback reads from a point behind
the newest input, so there is always processed audio ready.

```
                      +--------------- ffmpeg (one process) ---------------+
                      |  pre   = rumble high-pass + elliptical bass-mono   |
  input --+--> stdin -+  dc    = pre + declick                             +--> 4 pipes
          |           |  dn    = pre + denoise                             |
          |           |  both  = pre + declick + denoise                   |
          |           +---------------------------------------------------+
          |                              |
          +--> dry ring buffer           v  one ring buffer per output,
                      |                     indexed by absolute input sample
                      |                          |
                      +-------------+------------+
                                    v
                        playhead (fractional, resampled)
                                    |
                   switches select pre / dc / dn / both
                                    |
                        wet/dry blend --> output
                                    |
                 pre minus output = "removed", used by Monitor
                 mode, the meters and the spectrogram
```

All four variants run continuously, so the switches select between them
instantly with a short crossfade instead of restarting anything.

### Why FFmpeg runs out of process

The first version used PyAV in-process. Its filter-graph API leaks on every
push and pull, around 12 MB per hour per graph, unbounded, and rebuilding the
graph does not release it. Running FFmpeg as a subprocess isolates its memory
and keeps the filtering off Python's GIL. `tools/leak_isolate.py` and
`tools/pipe_test.py` are the measurements behind that decision.

### Alignment

FFmpeg's outputs do not all start on the same input sample: `adeclick` runs a
block early and `afftdn` a little late. Each instance is therefore fed silence
plus a short chirp before the live audio, and the chirp is located in every
output to measure that stream's offset. Offsets are measured on every start, so
nothing depends on hard-coded values.

The chirp only ever goes into FFmpeg, never to the speakers, and reads are
clamped so the startup padding cannot be played.
`tools/marker_leak_test.py` checks that by correlating the output against the
marker.

### Changing settings without a gap

`adeclick` and `adeclip` take no runtime commands, so a settings change needs a
new FFmpeg. Rather than restarting, the engine starts a second instance, waits
for it to align, and crossfades onto it. Measured: no unprocessed audio across a
settings change, against roughly 200 ms for a cold restart. That is what lets
the automatic tuning retune without being heard.

### Delay, and collapsing it

While filtering, playback runs behind the input far enough to cover FFmpeg's
lag. With both switches down the chain is bypassed, FFmpeg is stopped, and the
delay collapses to a few milliseconds.

Moving between the two is done by playing fractionally fast or slow and
resampling, never by skipping or repeating audio. The rates are asymmetric and
configurable: faster to build the delay so the declicker arrives sooner, slower
to shed it so the change is inaudible.

## Architecture

| File | Responsibility |
| --- | --- |
| `run.py` | Entry point. Serves the app with waitress. |
| `app.py` | HTTP routes and the API whitelist. |
| `audio_engine.py` | Real-time callback, playhead and resampler, variant selection, wet/dry blend, meters, spectrum, automatic tuning, guards, device selection, stream watchdog. |
| `dsp.py` | The FFmpeg process and its pipes, the ring buffer, marker alignment, hot-swap, and building the filter graph from settings. |
| `hardware_input.py` | Front panel serial listener. |
| `settings.py` | Defaults, environment overrides, atomic persistence. |
| `static/index.html` | The whole web UI, with no external dependencies. |
| `tests/` | Pytest suite, no audio hardware needed. |
| `tools/` | Harnesses that do need hardware or FFmpeg. See `tools/README.md`. |
| `firmware/` | Panel protocol and a reference Arduino sketch. |

## Behaviour contracts

The rules the engine is built around, and what the tests check:

- Never silence. Anything not ready in time, whether startup, a restart, a
  settings change or FFmpeg falling behind, plays dry audio instead.
- The panel wins. While it is connected the API refuses to change its three
  switches, so the hardware and the page cannot disagree. Unplugged, the API
  accepts them again.
- Switch changes are instant and crossfaded; settings changes are gapless.
- The filters never run away. If FFmpeg falls behind for long enough the
  expensive options are switched off and the reason is reported. Declick cost
  depends on the audio as well as the settings: on a near-silent input
  `adeclick` finds impulses everywhere.
- Removal is judged by energy, not peaks. Repairing one click is a large peak
  difference and inaudible.
- Monitor mode never persists across a restart, so the box cannot start up
  playing only the noise.

## HTTP API

| Endpoint | Purpose |
| --- | --- |
| `GET /` | The web UI. |
| `GET /api/state` | Settings plus live state: delay, speed, lag, alignment offsets, panel, guard messages. |
| `POST /api/state` | Change any whitelisted setting. Switch keys are refused while the panel is connected. |
| `GET /api/devices` | Input and output device names. `?refresh=1` re-queries the host. |
| `GET /api/meters` | Peak and RMS levels and the removal ratio. `?since=<seq>` also returns new spectrogram columns. |
| `GET /api/volume` | Master volume, where PipeWire is available. |
| `POST /api/volume` | Set master volume. Returns 503 without `wpctl`. |

## Setup

Needs Python 3.12 or newer, FFmpeg with `adeclick`, `adeclip` and `afftdn`, and
a full-duplex audio interface.

```bash
sudo apt install python3 python3-venv ffmpeg libportaudio2
git clone https://github.com/Zackeos/vinyl-declicker-pipeline
cd vinyl-declicker-pipeline
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python run.py
```

Then open `http://<host>:8080`.

To run it at boot, install the unit template as a user service so it shares the
audio session:

```bash
cp vinyl-declicker.service ~/.config/systemd/user/
sed -i "s|__INSTALL_DIR__|$PWD|" ~/.config/systemd/user/vinyl-declicker.service
systemctl --user daemon-reload
systemctl --user enable --now vinyl-declicker
sudo loginctl enable-linger "$USER"      # run without an active login
journalctl --user -u vinyl-declicker -f
```

## Configuration

Settings live in `settings.json`, are written atomically, and survive restarts.
Any key can be overridden with an environment variable named `VINYL_<KEY>`,
which takes precedence over the file:

```bash
VINYL_SERIAL_PORT=/dev/ttyUSB0 VINYL_PREFERRED_DEVICES='hw:1,0' ./venv/bin/python run.py
```

| Setting | Default | Notes |
| --- | --- | --- |
| `preferred_devices` | `["pipewire", "default"]` | Name fragments tried in order when no device is saved. Semicolon-separated in the environment, because ALSA names contain commas. |
| `serial_port`, `serial_baud` | `/dev/ttyACM0`, `9600` | The panel is optional. |
| `sample_rate` | `48000` | Match the sound server to avoid resampling. |
| `latency_ms`, `idle_latency_ms` | `200`, `15` | Delay while filtering, and when bypassed. |
| `engage_rate_pct`, `release_rate_pct` | `2.0`, `0.5` | How fast the delay is built and shed. Higher engages sooner and shifts pitch more while ramping. |
| `two_stage`, `declip` | off | Content-dependent cost. The overload guard turns them off if FFmpeg falls behind. |
| `auto_noise_floor` | off | Estimating a floor from overall level drifts up into the music; a fixed floor near -60 dBFS is safer. |

## Measurements

Measured on the machine this was built for: Intel Core i3-4160T (2 cores, 4
threads), Ubuntu 24.04, PipeWire at 48 kHz, Behringer UMC202HD, 1024-sample
blocks, shipped defaults.

| | Bypassed | Declicker + denoiser |
| --- | --- | --- |
| CPU, app and FFmpeg together | 16% of one core | 68% of one core |
| Engine delay | 16 ms | 201 ms |
| FFmpeg lag behind the input | not running | 95-100 ms |
| Memory | 63 MB | 63 MB + 49 MB FFmpeg |
| Dropouts (xruns) | 0 | 0 |

**End-to-end latency** is the engine delay plus what the host reports for the
interface, which was 42.7 ms in and 42.7 ms out here:

- bypassed: **about 100 ms**
- declicking and denoising: **about 285 ms**

Method: CPU from `utime + stime` in `/proc` for the app and its FFmpeg child
over 15 s; delay, lag and dropouts from `/api/state`; interface latency as
reported by PortAudio. After each transition settled, the counter for
unprocessed audio stopped moving, meaning processed audio was flowing
continuously.

Two caveats worth stating plainly. The interface figure is what the host
reports rather than a loopback measurement, so the end-to-end numbers are close
estimates, not exact. And the CPU cost of declicking depends on the audio as
well as the settings, on a near-silent input `adeclick` finds impulses
everywhere and costs considerably more, which is why the overload guard exists
and why two-stage declicking is off by default.

For stability, the same engine ran 22 days in its private deployment without a
restart, sitting at 37 MB with the filters disengaged.

## Tests

```bash
./venv/bin/pip install pytest
./venv/bin/python -m pytest tests -q
```

No audio hardware required. The suite covers the ring buffer, filter-graph
construction from settings, settings persistence and environment overrides,
marker alignment on synthetic signals, the automatic guards, and panel parsing
through a fake serial port. Harnesses that do need hardware are in `tools/`.

## Licence

MIT, see `LICENSE`.
