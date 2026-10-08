"""Marker alignment: inject a known shift and check it is recovered."""
from types import SimpleNamespace

import numpy as np
import pytest

from dsp import VARIANTS, Processor, Timeline, build_marker

SR = 48000


def make_instance(offset, channels=2, programme=0.2, with_marker=True):
    """A fake ffmpeg instance whose streams are shifted by `offset` samples."""
    preroll, marker = build_marker(SR, channels)
    m_len = marker.shape[0]
    pad = preroll.shape[0] - m_len
    base = 100_000
    rng = np.random.default_rng(0)

    timelines = {}
    for variant in VARIANTS:
        total = pad + m_len + int(programme * SR)
        buf = np.zeros((total, channels), dtype=np.float32)
        pos = pad + offset
        if with_marker:
            buf[pos:pos + m_len] = marker
        tail = pos + m_len
        buf[tail:] = (0.05 * rng.standard_normal((total - tail, channels))).astype(np.float32)
        tl = Timeline(1 << 18, channels, base - preroll.shape[0])
        tl.write(buf)
        timelines[variant] = tl

    return SimpleNamespace(marker=marker, preroll=preroll, base=base,
                           timelines=timelines, offsets={}, aligned=False)


def make_processor(channels=2):
    proc = Processor.__new__(Processor)        # no threads, no ffmpeg
    proc.sample_rate = SR
    proc.channels = channels
    proc.max_offset = int(0.1 * SR)
    return proc


@pytest.mark.parametrize("offset", [0, -1024, 176, -512, 2048])
def test_recovers_the_injected_shift_exactly(offset):
    proc, inst = make_processor(), make_instance(offset)
    proc._try_align(inst)
    assert inst.aligned
    assert set(inst.offsets) == set(VARIANTS)
    assert all(found == offset for found in inst.offsets.values()), inst.offsets


def test_real_world_offsets_are_recovered():
    """The values measured against this ffmpeg build: adeclick early, afftdn late."""
    proc = make_processor()
    inst = make_instance(-1024)
    proc._try_align(inst)
    assert inst.offsets["dc"] == -1024
    inst2 = make_instance(176)
    proc._try_align(inst2)
    assert inst2.offsets["dn"] == 176


def test_refuses_to_align_without_a_marker():
    """No chirp means no claim: better dry audio than a wrong offset."""
    proc, inst = make_processor(), make_instance(0, with_marker=False)
    proc._try_align(inst)
    assert not inst.aligned
    assert inst.offsets == {}


def test_waits_until_enough_output_exists():
    proc, inst = make_processor(), make_instance(0, programme=0.0)
    for tl in inst.timelines.values():          # truncate to below the probe window
        tl.end = tl.start + 100
    proc._try_align(inst)
    assert not inst.aligned


def test_a_shift_beyond_the_search_window_is_rejected():
    """A marker outside the window cannot be trusted, so no offset is claimed."""
    proc = make_processor()
    proc.max_offset = 1000
    inst = make_instance(5000)                  # well outside the search window
    proc._try_align(inst)
    assert not inst.aligned
    assert inst.offsets == {}
