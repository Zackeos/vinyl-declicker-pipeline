"""The automatic guards, driven one tuner pass at a time with seeded stats."""
import collections


import audio_engine
from audio_engine import Engine


class FakeProcessor:
    def __init__(self, lag_samples=0):
        self._lag = lag_samples

    def lag_samples(self, index):
        return self._lag

    def set_config(self, cfg):
        pass


def make_engine(lag_ms=0.0, **over):
    import settings
    opts = dict(settings.DEFAULTS)
    opts.update(over)
    eng = Engine.__new__(Engine)               # no threads, no audio device
    eng.opts = opts
    eng.running = True
    eng.monitor = False
    eng._index = 10 ** 6
    eng._play = 0.0
    eng._proc = FakeProcessor(int(opts["sample_rate"] * lag_ms / 1000))
    eng.auto_state = {}
    eng._block_levels = collections.deque(maxlen=4000)
    eng._removed_ratio = collections.deque(maxlen=4000)
    eng._last_floor_change = 0.0
    eng._last_sens_change = 0.0
    eng._overload_for = 0.0
    eng.applied = []
    eng.set_options = lambda changes, from_panel=False: eng.applied.append(changes)
    return eng


def tune_until_overload(eng):
    """The guard needs the condition to persist, so drive several passes."""
    for _ in range(int(audio_engine.OVERLOAD_SECONDS / audio_engine.TUNE_PERIOD) + 1):
        eng._tune_once()


# ---------------------------------------------------------------- overload ---

def test_sustained_lag_sheds_the_most_expensive_option_first():
    eng = make_engine(lag_ms=180, latency_ms=200, declip=True, two_stage=True)
    tune_until_overload(eng)
    assert eng.applied == [{"declip": False}]
    assert "declip" in eng.auto_state["overload"]


def test_two_stage_goes_next():
    eng = make_engine(lag_ms=180, latency_ms=200, declip=False, two_stage=True)
    tune_until_overload(eng)
    assert eng.applied == [{"two_stage": False}]


def test_nothing_left_to_shed_is_reported_not_silently_ignored():
    eng = make_engine(lag_ms=180, latency_ms=200, declip=False, two_stage=False)
    tune_until_overload(eng)
    assert eng.applied == []
    assert "behind" in eng.auto_state["overload"]


def test_a_brief_lag_spike_is_tolerated():
    eng = make_engine(lag_ms=180, latency_ms=200, declip=True, two_stage=True)
    eng._tune_once()                            # one pass only: not sustained
    assert eng.applied == []


def test_healthy_lag_clears_the_warning():
    eng = make_engine(lag_ms=60, latency_ms=200, declip=True)
    for _ in range(5):
        eng._tune_once()
    assert eng.applied == []
    assert "overload" not in eng.auto_state


# ------------------------------------------------------------- destruction ---

def test_removing_too_much_pulls_the_noise_floor_down():
    eng = make_engine(denoiser_on=True, noise_floor=-60.0)
    eng._removed_ratio.extend([-5.0] * 500)     # removed signal nearly as loud as the music
    eng._tune_once()
    assert eng.applied, "the guard should have intervened"
    change = eng.applied[0]
    assert change["noise_floor"] == -65.0
    assert change["auto_noise_floor"] is False


def test_the_floor_is_not_pulled_below_its_limit():
    eng = make_engine(denoiser_on=True, noise_floor=-74.0)
    eng._removed_ratio.extend([-5.0] * 500)
    eng._tune_once()
    assert eng.applied[0]["noise_floor"] == -75.0


def test_gentle_removal_is_left_alone():
    eng = make_engine(denoiser_on=True, noise_floor=-60.0)
    eng._removed_ratio.extend([-52.0] * 500)    # healthy
    eng._tune_once()
    assert all("noise_floor" not in c for c in eng.applied)


# -------------------------------------------------------------- sensitivity ---

def test_sensitivity_rises_when_almost_nothing_is_removed():
    eng = make_engine(auto_sensitivity=True, declicker_on=True, sensitivity=5.0)
    eng._removed_ratio.extend([-70.0] * 500)
    eng._tune_once()
    assert eng.applied == [{"sensitivity": 5.5}]


def test_sensitivity_falls_when_too_much_is_removed():
    eng = make_engine(auto_sensitivity=True, declicker_on=True, sensitivity=5.0)
    eng._removed_ratio.extend([-20.0] * 500)
    eng._tune_once()
    assert eng.applied == [{"sensitivity": 4.5}]


def test_sensitivity_is_left_alone_in_the_healthy_band():
    eng = make_engine(auto_sensitivity=True, declicker_on=True, sensitivity=5.0)
    eng._removed_ratio.extend([-45.0] * 500)
    eng._tune_once()
    assert eng.applied == []


def test_sensitivity_is_not_touched_when_auto_is_off():
    eng = make_engine(auto_sensitivity=False, declicker_on=True)
    eng._removed_ratio.extend([-70.0] * 500)
    eng._tune_once()
    assert eng.applied == []


# -------------------------------------------------------------- noise floor ---

def test_the_floor_is_only_trusted_with_real_headroom():
    """On a loud continuous side the quiet percentile is music, not the floor."""
    eng = make_engine(auto_noise_floor=True)
    eng._block_levels.extend([-20.0] * 2000)    # no gap between floor and programme
    eng._tune_once()
    assert all("noise_floor" not in c for c in eng.applied)
    assert eng.auto_state.get("floor_usable") is False


def test_a_genuine_gap_is_measured_and_applied():
    eng = make_engine(auto_noise_floor=True, noise_floor=-60.0)
    eng._block_levels.extend([-72.0] * 200)     # quiet groove
    eng._block_levels.extend([-15.0] * 1800)    # music well above it
    eng._tune_once()
    floors = [c["noise_floor"] for c in eng.applied if "noise_floor" in c]
    assert floors and floors[0] <= audio_engine.FLOOR_CEILING


def test_the_floor_never_rises_above_the_ceiling():
    eng = make_engine(auto_noise_floor=True, noise_floor=-80.0)
    eng._block_levels.extend([-55.0] * 200)
    eng._block_levels.extend([-5.0] * 1800)
    eng._tune_once()
    for change in eng.applied:
        if "noise_floor" in change:
            assert change["noise_floor"] <= audio_engine.FLOOR_CEILING
