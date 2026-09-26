import asyncio
import socket

import aiohttp
import pytest

from tonearm.owntone import Output, OwnToneClient, OwnToneError
from tests.fake_owntone import FakeOwnTone


@pytest.fixture
async def fake(aiohttp_server):
    fake = FakeOwnTone()
    server = await aiohttp_server(fake.app())
    fake.ws_port = server.port
    fake.url = f"http://127.0.0.1:{server.port}"
    return fake


@pytest.fixture
async def client(fake):
    async with aiohttp.ClientSession() as session:
        yield OwnToneClient(fake.url, session)


async def test_outputs_are_parsed(client):
    outputs = await client.outputs()
    assert [o.name for o in outputs] == ["Bedroom", "Living Room", "Computer"]
    assert outputs[1] == Output(id="266176521832340", name="Living Room", type="AirPlay 2",
                                selected=False, volume=40, needs_pin=True)
    assert outputs[2].selected is True
    assert outputs[2].id == "0"


async def test_set_outputs(client, fake):
    await client.set_outputs(["1", "2"])
    assert fake.requests[-1] == ("PUT", "/api/outputs/set", {}, {"outputs": ["1", "2"]})


async def test_update_output_sends_only_given_fields(client, fake):
    await client.update_output("5", volume=30)
    assert fake.requests[-1] == ("PUT", "/api/outputs/5", {}, {"volume": 30})
    await client.update_output("5", pin="1234")
    assert fake.requests[-1][3] == {"pin": "1234"}
    await client.update_output("5", selected=False)
    assert fake.requests[-1][3] == {"selected": False}


async def test_find_track_uri(client, fake):
    assert await client.find_track_uri("/srv/music/Turntable") == "library:track:1"
    method, path, query, _ = fake.requests[-1]
    assert (method, path) == ("GET", "/api/search")
    assert query == {"type": "tracks", "expression": 'path is "/srv/music/Turntable"'}
    fake.tracks = []
    assert await client.find_track_uri("/srv/music/Turntable") is None


async def test_play_uri_clears_queue_and_starts(client, fake):
    await client.play_uri("library:track:1")
    assert fake.requests[-1] == ("POST", "/api/queue/items/add",
                                 {"uris": "library:track:1", "clear": "true", "playback": "start"}, None)


async def test_play_uri_survives_slow_owntone_within_play_timeout(fake):
    fake.delay_s = 0.4
    async with aiohttp.ClientSession() as session:
        client = OwnToneClient(fake.url, session, timeout_s=0.2, play_timeout_s=1.0)
        await client.play_uri("library:track:1")


async def test_other_calls_still_use_short_timeout(fake):
    fake.delay_s = 0.4
    async with aiohttp.ClientSession() as session:
        client = OwnToneClient(fake.url, session, timeout_s=0.2, play_timeout_s=1.0)
        with pytest.raises(OwnToneError):
            await client.set_outputs(["1"])


async def test_stop(client, fake):
    await client.stop()
    assert fake.requests[-1][:2] == ("PUT", "/api/player/stop")


async def test_player_state(client, fake):
    fake.player = "play"
    assert await client.player_state() == "play"


async def test_now_playing_path(client, fake):
    assert await client.now_playing_path() is None
    fake.item_id = 42
    fake.queue_items = [{"id": 42, "path": "/srv/music/Turntable"}]
    assert await client.now_playing_path() == "/srv/music/Turntable"
    fake.item_id = 43
    assert await client.now_playing_path() is None


async def test_http_error_raises(client, fake):
    fake.fail_status = 500
    with pytest.raises(OwnToneError):
        await client.outputs()


async def test_unreachable_raises():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    async with aiohttp.ClientSession() as session:
        with pytest.raises(OwnToneError):
            await OwnToneClient(f"http://127.0.0.1:{port}", session).outputs()


async def test_notifications(client, fake):
    events = client.notifications()
    next_event = asyncio.ensure_future(events.__anext__())
    await asyncio.sleep(0.1)
    fake.notify_queue.put_nowait(["outputs"])
    assert await asyncio.wait_for(next_event, 2) == ["outputs"]
    assert fake.subscribed == {"notify": ["outputs", "player", "volume"]}
    fake.notify_queue.put_nowait(None)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(events.__anext__(), 2)


async def test_invalid_json_raises(client, fake):
    fake.raw_outputs_body = "not json"
    with pytest.raises(OwnToneError):
        await client.outputs()


async def test_malformed_output_entry_raises(client, fake):
    fake.raw_outputs_body = '{"outputs": [{"name": "no id"}]}'
    with pytest.raises(OwnToneError):
        await client.outputs()


async def test_missing_websocket_port_raises(client, fake):
    fake.config_body = "{}"
    events = client.notifications()
    with pytest.raises(OwnToneError):
        await events.__anext__()
