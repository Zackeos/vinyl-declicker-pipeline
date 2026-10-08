"""The ring buffer that every processed stream is read through."""
import numpy as np

from dsp import Timeline


def ramp(n, channels=2, start=0):
    v = np.arange(start, start + n, dtype=np.float32)
    return np.column_stack([v] * channels)


def test_reads_back_what_was_written():
    tl = Timeline(1024, 2, 0)
    tl.write(ramp(100))
    out = np.zeros((100, 2), dtype=np.float32)
    assert tl.read_into(0, out) == (0, 100)
    assert np.array_equal(out, ramp(100))


def test_absolute_indexing_survives_wraparound():
    tl = Timeline(256, 2, 0)
    for i in range(10):                        # 1000 samples through a 256 buffer
        tl.write(ramp(100, start=i * 100))
    out = np.zeros((50, 2), dtype=np.float32)
    assert tl.read_into(950, out) == (0, 50)
    assert np.array_equal(out, ramp(50, start=950))


def test_requesting_the_future_returns_nothing():
    tl = Timeline(256, 2, 0)
    tl.write(ramp(100))
    out = np.zeros((10, 2), dtype=np.float32)
    assert tl.read_into(500, out) == (0, 0)


def test_partial_coverage_reports_the_filled_span():
    """A read straddling the newest sample fills only the part that exists."""
    tl = Timeline(256, 2, 0)
    tl.write(ramp(100))
    out = np.zeros((40, 2), dtype=np.float32)
    assert tl.read_into(80, out) == (0, 20)    # 80..100 exists, 100..120 does not
    assert np.array_equal(out[:20], ramp(20, start=80))


def test_data_older_than_the_buffer_is_gone():
    tl = Timeline(256, 2, 0)
    for i in range(10):
        tl.write(ramp(100, start=i * 100))
    out = np.zeros((10, 2), dtype=np.float32)
    assert tl.read_into(0, out) == (0, 0)


def test_a_write_larger_than_the_buffer_keeps_the_newest():
    tl = Timeline(256, 2, 0)
    tl.write(ramp(1000))
    assert tl.end == 1000
    out = np.zeros((256, 2), dtype=np.float32)
    assert tl.read_into(744, out) == (0, 256)
    assert np.array_equal(out, ramp(256, start=744))


def test_reads_starting_before_the_history_are_offset_correctly():
    """lo marks where in the output buffer the available data begins."""
    tl = Timeline(1024, 2, 0)
    tl.write(ramp(100, start=0))
    out = np.zeros((20, 2), dtype=np.float32)
    lo, hi = tl.read_into(-5, out)             # 5 samples before anything exists
    assert (lo, hi) == (5, 20)
    assert np.array_equal(out[5:20], ramp(15, start=0))
