"""Settings must survive a restart, and never be left half-written."""
import json
import os

import pytest

import settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))
    for key in list(os.environ):
        if key.startswith("VINYL_"):
            monkeypatch.delenv(key)
    return path


def test_defaults_when_nothing_is_saved(store):
    assert settings.load() == settings.DEFAULTS


def test_saved_values_override_defaults(store):
    store.write_text(json.dumps({"sensitivity": 8.5}))
    assert settings.load()["sensitivity"] == 8.5


def test_unknown_keys_are_ignored(store):
    store.write_text(json.dumps({"sensitivity": 3.0, "nonsense": 1}))
    data = settings.load()
    assert data["sensitivity"] == 3.0
    assert "nonsense" not in data


def test_a_corrupt_file_falls_back_to_defaults(store):
    store.write_text("{ not json")
    assert settings.load() == settings.DEFAULTS


def test_write_is_atomic(store):
    settings._write(dict(settings.DEFAULTS))
    assert store.exists()
    assert not (store.parent / "settings.json.tmp").exists()
    assert json.loads(store.read_text())["sample_rate"] == 48000


def test_env_overrides_are_coerced_by_type(store, monkeypatch):
    monkeypatch.setenv("VINYL_SERIAL_PORT", "/dev/ttyUSB1")
    monkeypatch.setenv("VINYL_SERIAL_BAUD", "115200")
    monkeypatch.setenv("VINYL_WET", "0.5")
    monkeypatch.setenv("VINYL_IDLE_FFMPEG", "false")
    # Semicolons, because ALSA device names contain commas.
    monkeypatch.setenv("VINYL_PREFERRED_DEVICES", "hw:1,0; pipewire")
    data = settings.load()
    assert data["serial_port"] == "/dev/ttyUSB1"
    assert data["serial_baud"] == 115200
    assert data["wet"] == 0.5
    assert data["idle_ffmpeg"] is False
    assert data["preferred_devices"] == ["hw:1,0", "pipewire"]


def test_env_beats_the_saved_file(store, monkeypatch):
    store.write_text(json.dumps({"serial_port": "/dev/ttyACM9"}))
    monkeypatch.setenv("VINYL_SERIAL_PORT", "/dev/ttyUSB0")
    assert settings.load()["serial_port"] == "/dev/ttyUSB0"


def test_a_bad_env_value_is_ignored_rather_than_fatal(store, monkeypatch):
    monkeypatch.setenv("VINYL_SAMPLE_RATE", "not-a-number")
    assert settings.load()["sample_rate"] == settings.DEFAULTS["sample_rate"]


def test_save_soon_writes_only_known_keys(store, monkeypatch):
    data = dict(settings.DEFAULTS)
    data["sensitivity"] = 9.0
    data["transient"] = "should not be stored"
    settings.save_soon(data, delay=0.01)
    import time
    time.sleep(0.3)
    saved = json.loads(store.read_text())
    assert saved["sensitivity"] == 9.0
    assert "transient" not in saved
