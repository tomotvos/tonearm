import array

import pytest

from tonearm.config import Config
from tonearm.service import Service
from tonearm.settings import Settings
from tests.fakes import FakeCapture, FakeClient, FakeClock, FakePipe

LOUD_CHUNK = array.array("h", [3000] * 4410).tobytes()
SILENT_CHUNK = bytes(8820)
INPUTS = [{"id": "plughw:0,0", "name": "Intel"}, {"id": "plughw:1,0", "name": "USB AUDIO  CODEC"}]


async def fake_list_inputs():
    return list(INPUTS)


def feed(svc, clock, chunk, seconds):
    for _ in range(int(round(seconds / 0.05))):
        clock.advance(0.05)
        svc.capture.on_chunk(chunk)


@pytest.fixture
def make_service(tmp_path):
    def make(settings=None, list_inputs=None, **env):
        config = Config.from_env({
            "TONEARM_SETTINGS": str(tmp_path / "settings.json"),
            "TONEARM_START_DB": "-60",
            "TONEARM_FLOOR_DB": "-75",
            **env,
        })
        client, pipe, clock = FakeClient(), FakePipe(), FakeClock()
        if settings is None:
            settings = Settings(input="plughw:1,0", default_speakers=["1", "2"])
        svc = Service(config, client, pipe, settings, capture_factory=FakeCapture,
                      list_inputs=list_inputs or fake_list_inputs, clock=clock)
        svc.capture.on_available(True)
        return svc, client, pipe, clock
    return make
