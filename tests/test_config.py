from pathlib import Path

import pytest

from tonearm.config import Config


def test_defaults():
    c = Config.from_env({})
    assert c.owntone_url == "http://localhost:3689"
    assert c.pipe_path == "/srv/music/Turntable"
    assert c.quiet_timeout_s == 180.0
    assert c.start_db == -65.0
    assert c.floor_db == -80.0
    assert c.settings_path == Path("~/.config/tonearm/settings.json").expanduser()
    assert c.port == 8080
    assert c.web_dir.name == "web"


def test_overrides():
    c = Config.from_env({
        "OWNTONE_URL": "http://pi:3689/",
        "TONEARM_PIPE": "/tmp/p",
        "TONEARM_QUIET_TIMEOUT_S": "30",
        "TONEARM_START_DB": "-50",
        "TONEARM_FLOOR_DB": "-70",
        "TONEARM_SETTINGS": "/tmp/s.json",
        "TONEARM_PORT": "9000",
    })
    assert c.owntone_url == "http://pi:3689"
    assert c.pipe_path == "/tmp/p"
    assert c.quiet_timeout_s == 30.0
    assert (c.start_db, c.floor_db) == (-50.0, -70.0)
    assert c.settings_path == Path("/tmp/s.json")
    assert c.port == 9000


def test_start_must_exceed_floor():
    with pytest.raises(ValueError):
        Config.from_env({"TONEARM_START_DB": "-80", "TONEARM_FLOOR_DB": "-70"})
