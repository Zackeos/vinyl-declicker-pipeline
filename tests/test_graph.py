"""The ffmpeg filter graph is built from settings, so settings must shape it."""
import settings
from dsp import VARIANTS, build_graph, declick_threshold


def cfg(**over):
    c = dict(settings.DEFAULTS)
    c.update(over)
    return c


def test_every_variant_is_produced():
    g = build_graph(cfg())
    for v in VARIANTS:
        assert f"[{v}]" in g, f"variant {v} missing from graph"


def test_sensitivity_sets_the_declick_threshold():
    assert f"threshold={declick_threshold(0.0):.2f}" in build_graph(cfg(sensitivity=0.0))
    assert f"threshold={declick_threshold(10.0):.2f}" in build_graph(cfg(sensitivity=10.0))


def test_sensitivity_mapping_is_monotonic_and_clamped():
    assert declick_threshold(0) > declick_threshold(5) > declick_threshold(10)
    assert declick_threshold(100) == 1.5          # clamped, never lower


def test_two_stage_adds_a_second_declick_pass():
    assert build_graph(cfg(two_stage=False)).count("adeclick") == 1
    assert build_graph(cfg(two_stage=True)).count("adeclick") == 2


def test_declip_is_optional():
    assert "adeclip" not in build_graph(cfg(declip=False))
    assert "adeclip" in build_graph(cfg(declip=True))


def test_rumble_and_bass_mono_can_be_switched_off():
    g = build_graph(cfg(rumble=True, rumble_hz=18, bass_mono=True, bass_mono_hz=150))
    assert "highpass=f=18" in g and "highpass=f=150" in g
    plain = build_graph(cfg(rumble=False, bass_mono=False))
    assert "highpass" not in plain and "amerge" not in plain


def test_bass_mono_filters_only_the_side_channel():
    """Filtering mid as well would stop it being phase-coherent."""
    g = build_graph(cfg(bass_mono=True))
    assert "pan=mono|c0=0.5*c0+0.5*c1[mid]" in g
    assert "pan=mono|c0=0.5*c0-0.5*c1,highpass" in g


def test_denoiser_parameters_reach_the_graph():
    assert "afftdn=nr=7.50:nf=-55.0:tn=1" in build_graph(
        cfg(strength=7.5, noise_floor=-55, track_noise=True))
    assert "tn=0" in build_graph(cfg(track_noise=False))


def test_noise_floor_is_clamped_to_what_afftdn_accepts():
    assert "nf=-80.0" in build_graph(cfg(noise_floor=-200))
    assert "nf=-20.0" in build_graph(cfg(noise_floor=0))
