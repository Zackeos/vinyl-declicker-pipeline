"""Front panel parsing, and the listener driven by a fake serial port."""
import pytest

import hardware_input
from hardware_input import KEYS, parse_switch_state


def test_parses_a_full_reading():
    assert parse_switch_state("Switch 1: ON  | Switch 2: OFF | Switch 3: ON") == (True, False, True)


def test_is_case_and_space_insensitive():
    assert parse_switch_state("switch 1:on|switch 2:on|switch 3:off") == (True, True, False)


@pytest.mark.parametrize("line", [
    "",
    "hello",
    "Switch 1: ON",
    "Switch 1: ON | Switch 2: OFF",
    "Switch 1: MAYBE | Switch 2: OFF | Switch 3: OFF",
])
def test_incomplete_or_malformed_lines_are_rejected(line):
    assert parse_switch_state(line) is None


class FakeSerial:
    """Feeds canned lines, then stops the listener loop by raising."""

    class Stop(BaseException):
        """Not an Exception: the listener catches those broadly."""

    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        if not self.lines:
            raise FakeSerial.Stop()
        return (self.lines.pop(0) + "\n").encode()

    def close(self):
        pass


class FakeEngine:
    def __init__(self):
        self.panel = {"connected": False, "switches": None, "raw": None}
        self.opts = {"serial_port": "/dev/null", "serial_baud": 9600}
        self.applied = []

    def set_options(self, changes, from_panel=False):
        assert from_panel, "panel changes must be marked as coming from the panel"
        self.applied.append(changes)


def run_listener(monkeypatch, lines):
    engine = FakeEngine()
    fake = FakeSerial(lines)
    monkeypatch.setattr(hardware_input.serial, "Serial", lambda *a, **k: fake)
    with pytest.raises(FakeSerial.Stop):
        hardware_input.serial_listener(engine)
    return engine


def test_first_reading_is_applied_in_full(monkeypatch):
    engine = run_listener(monkeypatch, ["Switch 1: ON | Switch 2: OFF | Switch 3: OFF"])
    assert engine.applied == [dict(zip(KEYS, (True, False, False)))]
    assert engine.panel["connected"] is True


def test_only_changes_are_applied(monkeypatch):
    engine = run_listener(monkeypatch, [
        "Switch 1: ON | Switch 2: OFF | Switch 3: OFF",
        "Switch 1: ON | Switch 2: OFF | Switch 3: OFF",   # identical, no update
        "Switch 1: ON | Switch 2: ON  | Switch 3: OFF",   # one switch moved
    ])
    assert engine.applied == [
        dict(zip(KEYS, (True, False, False))),
        {"denoiser_on": True},
    ]


def test_junk_between_readings_is_skipped(monkeypatch):
    engine = run_listener(monkeypatch, [
        "booting",
        "",
        "Switch 1: OFF | Switch 2: OFF | Switch 3: ON",
    ])
    assert engine.applied == [dict(zip(KEYS, (False, False, True)))]
    assert engine.panel["raw"] == "Switch 1: OFF | Switch 2: OFF | Switch 3: ON"
