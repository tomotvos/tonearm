import asyncio
import contextlib
import json

import pytest

from tonearm.api import create_app


@pytest.fixture
async def api(aiohttp_client, make_service, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<h1>tonearm</h1>")
    (web_dir / "app.js").write_text("console.log('hi')")
    svc, client, pipe, clock = make_service()
    return await aiohttp_client(create_app(svc, web_dir)), svc, client


async def test_status_returns_snapshot(api):
    http, svc, client = api
    resp = await http.get("/api/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["state"] == "armed"
    assert set(data) >= {"auto_on", "countdown_s", "level_db", "speakers", "owntone_up", "error"}


async def test_play_with_speakers(api):
    http, svc, client = api
    resp = await http.post("/api/play", json={"speakers": ["3"]})
    assert resp.status == 200
    assert (await resp.json())["state"] == "playing"
    assert ("set_outputs", ["3"]) in client.calls


async def test_play_with_mixed_available_and_unavailable_speakers(api):
    http, svc, client = api
    await svc.refresh_outputs()
    resp = await http.post("/api/play", json={"speakers": ["1", "9"]})
    assert resp.status == 200
    assert ("set_outputs", ["1"]) in client.calls


async def test_play_with_only_unavailable_speaker_reports_no_speakers_error(api):
    http, svc, client = api
    await svc.refresh_outputs()
    resp = await http.post("/api/play", json={"speakers": ["9"]})
    assert resp.status == 200
    assert not any(c[0] == "set_outputs" for c in client.calls)
    assert "speakers" in (await resp.json())["error"].lower()


async def test_play_without_body_uses_defaults(api):
    http, svc, client = api
    resp = await http.post("/api/play")
    assert resp.status == 200
    assert ("set_outputs", ["1", "2"]) in client.calls


async def test_play_rejects_bad_speakers(api):
    http, svc, client = api
    resp = await http.post("/api/play", json={"speakers": "3"})
    assert resp.status == 400
    assert "speakers" in (await resp.json())["error"]


async def test_invalid_json_is_rejected(api):
    http, svc, client = api
    resp = await http.post("/api/play", data="{bad", headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_non_object_body_is_rejected(api):
    http, svc, client = api
    resp = await http.put("/api/auto-on", json=[True])
    assert resp.status == 400


async def test_play_is_shielded_from_client_disconnect(api):
    http, svc, client = api
    completed = asyncio.Event()

    async def fake_play(speakers=None):
        await asyncio.sleep(0.05)
        completed.set()

    svc.play = fake_play
    request_task = asyncio.ensure_future(http.post("/api/play"))
    await asyncio.sleep(0.01)
    request_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await request_task
    await asyncio.wait_for(completed.wait(), 1)


async def test_stop_holds(api):
    http, svc, client = api
    await http.post("/api/play")
    resp = await http.post("/api/stop")
    assert (await resp.json())["state"] == "held"


async def test_auto_on(api):
    http, svc, client = api
    assert (await http.put("/api/auto-on", json={"enabled": "no"})).status == 400
    resp = await http.put("/api/auto-on", json={"enabled": False})
    assert resp.status == 200
    data = await resp.json()
    assert data["auto_on"] is False
    assert data["state"] == "off"


@pytest.mark.parametrize("body", [{"volume": 150}, {"volume": -1}, {"volume": True}, {"volume": "30"},
                                  {"pin": "12a4"}, {"pin": 1234}, {"pin": "123"},
                                  {"selected": "yes"}, {}])
async def test_speaker_validation(api, body):
    http, svc, client = api
    resp = await http.put("/api/speakers/1", json=body)
    assert resp.status == 400


async def test_speaker_volume(api):
    http, svc, client = api
    resp = await http.put("/api/speakers/1", json={"volume": 30})
    assert resp.status == 200
    assert ("update_output", "1", {"volume": 30}) in client.calls


async def test_speaker_owntone_failure_is_502(api):
    http, svc, client = api
    client.fail = True
    resp = await http.put("/api/speakers/1", json={"volume": 30})
    assert resp.status == 502
    assert (await resp.json())["error"] == "OwnTone down"


async def test_speaker_unavailable_is_502(api):
    http, svc, client = api
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "1"]
    await svc.refresh_outputs()
    resp = await http.put("/api/speakers/1", json={"volume": 30})
    assert resp.status == 502
    assert "no longer available" in (await resp.json())["error"]


async def test_default_speakers(api):
    http, svc, client = api
    assert (await http.put("/api/default-speakers", json={"ids": [1]})).status == 400
    resp = await http.put("/api/default-speakers", json={"ids": ["2"]})
    assert (await resp.json())["default_speakers"] == ["2"]


async def test_inputs(api):
    http, svc, client = api
    resp = await http.get("/api/inputs")
    inputs = (await resp.json())["inputs"]
    assert [i["id"] for i in inputs] == ["plughw:0,0", "plughw:1,0"]
    assert all("legacy_id" not in i for i in inputs)


async def test_set_input(api):
    http, svc, client = api
    assert (await http.put("/api/input", json={"id": "plughw:9,0"})).status == 400
    resp = await http.put("/api/input", json={"id": "plughw:0,0"})
    assert (await resp.json())["input"] == "plughw:0,0"
    assert svc.capture.starts[-1] == "plughw:0,0"


@pytest.mark.parametrize("body", [{"id": []}, {"id": {}}, {"id": 1}])
async def test_set_input_rejects_non_string_id(api, body):
    http, svc, client = api
    resp = await http.put("/api/input", json=body)
    assert resp.status == 400


async def test_speaker_rejects_non_numeric_id(api):
    http, svc, client = api
    resp = await http.put("/api/speakers/..%2Fplayer%2Fstop", json={"volume": 30})
    assert resp.status == 400
    assert not any(call[0] == "update_output" for call in client.calls)


async def test_index_and_static_are_served(api):
    http, svc, client = api
    assert "tonearm" in await (await http.get("/")).text()
    assert "console.log" in await (await http.get("/static/app.js")).text()


async def read_event(resp):
    while True:
        line = await asyncio.wait_for(resp.content.readline(), 2)
        if line.startswith(b"data: "):
            return json.loads(line[len(b"data: "):])


async def test_events_stream_snapshots(api):
    http, svc, client = api
    resp = await http.get("/api/events")
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert (await read_event(resp))["state"] == "armed"
    await http.post("/api/play")
    for _ in range(5):
        if (await read_event(resp))["state"] == "playing":
            break
    else:
        raise AssertionError("never saw the playing state")
    resp.close()
