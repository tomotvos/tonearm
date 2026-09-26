import asyncio
import dataclasses

from tonearm.owntone import Output, OwnToneError


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakePipe:
    def __init__(self):
        self.is_open = False
        self.writes = []
        self.opens = 0

    def open(self):
        self.is_open = True
        self.opens += 1

    def write(self, chunk):
        if self.is_open:
            self.writes.append(chunk)

    def close(self):
        self.is_open = False


class FakeCapture:
    def __init__(self, on_chunk, on_available):
        self.on_chunk = on_chunk
        self.on_available = on_available
        self.starts = []
        self.stops = 0

    def start(self, device):
        self.starts.append(device)

    async def stop(self):
        self.stops += 1


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.track_uri = "library:track:1"
        self.find_error = None
        self.hold = None
        self.hold_play = None
        self.state = "play"
        self.now_path = None
        self.notify_fail = False
        self.notify = asyncio.Queue()
        self.outputs_list = [
            Output("1", "Kitchen", "AirPlay 2", False, 50, False),
            Output("2", "Den", "AirPlay 2", False, 50, False),
            Output("3", "Living Room", "AirPlay 2", False, 40, True),
        ]

    def _call(self, *call):
        self.calls.append(call)
        if self.fail:
            raise OwnToneError("OwnTone down")

    async def outputs(self):
        self._call("outputs")
        return list(self.outputs_list)

    async def set_outputs(self, ids):
        self._call("set_outputs", list(ids))
        if self.hold is not None:
            await self.hold.wait()
        self.outputs_list = [dataclasses.replace(o, selected=o.id in ids) for o in self.outputs_list]

    async def update_output(self, output_id, *, selected=None, volume=None, pin=None):
        fields = {k: v for k, v in (("selected", selected), ("volume", volume), ("pin", pin)) if v is not None}
        self._call("update_output", output_id, fields)
        changes = {k: v for k, v in fields.items() if k in ("selected", "volume")}
        if pin is not None:
            changes["needs_pin"] = False
        self.outputs_list = [dataclasses.replace(o, **changes) if o.id == output_id else o
                             for o in self.outputs_list]

    async def find_track_uri(self, path):
        self._call("find_track_uri", path)
        if self.find_error is not None:
            raise self.find_error
        return self.track_uri

    async def play_uri(self, uri):
        self._call("play_uri", uri)
        if self.hold_play is not None:
            await self.hold_play.wait()

    async def stop(self):
        self._call("stop")

    async def player_state(self):
        self._call("player_state")
        return self.state

    async def now_playing_path(self):
        self._call("now_playing_path")
        return self.now_path

    async def notifications(self):
        self._call("notifications")
        if self.notify_fail:
            raise OwnToneError("notifications down")
        while (events := await self.notify.get()) is not None:
            yield events
