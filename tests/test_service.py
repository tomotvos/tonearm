import array
import asyncio
import contextlib
import logging

import pytest

import tonearm.service as service_module
from tonearm.gate import State
from tonearm.owntone import Output, OwnToneError
from tonearm.settings import Settings, load
from tests.conftest import LOUD_CHUNK, SILENT_CHUNK, feed


class _StopLoop(Exception):
    pass


def distinct_loud_chunks(n, start):
    return [array.array("h", [start + i] * 4410).tobytes() for i in range(n)]


def feed_sequence(svc, clock, chunks):
    for chunk in chunks:
        clock.advance(0.05)
        svc.capture.on_chunk(chunk)


async def start_by_needle_drop(svc, clock):
    feed(svc, clock, LOUD_CHUNK, 1.1)
    await svc.drain()
    assert svc.gate.state is State.PLAYING


async def test_needle_drop_starts_playback_on_default_speakers(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    assert client.calls == [
        ("find_track_uri", "/srv/music/Turntable"),
        ("set_outputs", ["1", "2"]),
        ("play_uri", "library:track:1"),
    ]
    assert pipe.is_open
    assert len(pipe.writes) == 22
    feed(svc, clock, LOUD_CHUNK, 0.5)
    assert len(pipe.writes) == 32


async def test_auto_start_logs_decision_and_owntone_timeline(make_service, caplog):
    svc, client, pipe, clock = make_service()
    with caplog.at_level(logging.INFO, logger="tonearm.service"):
        await start_by_needle_drop(svc, clock)
    messages = [r.message for r in caplog.records]
    decided = [m for m in messages if m.startswith("start decided")]
    assert len(decided) == 1
    assert "auto=True" in decided[0]
    assert "speakers=['1', '2']" in decided[0]
    assert "gate_state=playing" in decided[0]
    assert "prebuffer_chunks=" in decided[0]
    assert any(m.startswith("owntone accepted set_outputs after") for m in messages)
    assert any(m.startswith("owntone accepted play_uri after") for m in messages)


async def test_manual_play_logs_decision(make_service, caplog):
    svc, client, pipe, clock = make_service()
    with caplog.at_level(logging.INFO, logger="tonearm.service"):
        await svc.play(["3"])
    decided = [r.message for r in caplog.records if r.message.startswith("start decided")]
    assert len(decided) == 1
    assert "auto=False" in decided[0]
    assert "speakers=['3']" in decided[0]


async def test_auto_start_without_default_speakers_reports_error(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=[]))
    await start_by_needle_drop(svc, clock)
    assert client.calls == []
    assert not pipe.is_open
    assert svc.snapshot()["error"] == service_module.AUTO_WAITING_FOR_SPEAKER_ERROR


async def test_auto_start_retries_while_no_default_speaker_available(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["9"]))
    await svc.refresh_outputs()
    client.calls.clear()
    await start_by_needle_drop(svc, clock)
    assert client.calls == []
    assert not pipe.is_open
    assert svc.snapshot()["error"] == service_module.AUTO_WAITING_FOR_SPEAKER_ERROR

    client.outputs_list.append(Output("9", "New Room", "AirPlay 2", False, 50, False))
    await svc.refresh_outputs()
    clock.advance(service_module.SPEAKER_APPEAR_DELAY_S + service_module.RETRY_INTERVAL_S)
    await svc.tick()
    await svc.drain()

    assert ("set_outputs", ["9"]) in client.calls
    assert pipe.is_open
    assert svc.snapshot()["error"] is None


async def test_stop_during_auto_retry_wait_ends_retries(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=[]))
    await start_by_needle_drop(svc, clock)
    assert svc._pending_auto is True
    await svc.stop()
    assert svc._pending_start is None
    assert svc._pending_auto is False
    clock.advance(service_module.RETRY_INTERVAL_S * 3)
    client.calls.clear()
    await svc.tick()
    await svc.drain()
    assert client.calls == []


async def test_quiet_timeout_during_auto_retry_wait_ends_retries(make_service):
    svc, client, pipe, clock = make_service(TONEARM_QUIET_TIMEOUT_S="10",
                                            settings=Settings(input="plughw:1,0", default_speakers=[]))
    await start_by_needle_drop(svc, clock)
    assert svc._pending_auto is True
    feed(svc, clock, SILENT_CHUNK, 10.5)
    await svc.drain()
    assert svc.gate.state is State.ARMED
    assert svc._pending_start is None
    assert svc._pending_auto is False
    clock.advance(service_module.RETRY_INTERVAL_S * 3)
    client.calls.clear()
    await svc.tick()
    await svc.drain()
    assert client.calls == []


async def test_manual_play_with_no_speakers_does_not_retry(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=[]))
    await svc.play()
    assert svc._pending_start is None
    assert svc._pending_auto is False
    clock.advance(service_module.RETRY_INTERVAL_S * 3)
    client.calls.clear()
    await svc.tick()
    await svc.drain()
    assert client.calls == []


async def test_auto_start_skips_offline_default_speakers(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["1", "9"]))
    await svc.refresh_outputs()
    await start_by_needle_drop(svc, clock)
    assert ("set_outputs", ["1"]) in client.calls


async def test_stop_while_playing_holds_and_releases(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    await svc.stop()
    assert svc.gate.state is State.HELD
    assert not pipe.is_open
    assert client.calls[-1] == ("stop",)
    feed(svc, clock, LOUD_CHUNK, 2.0)
    await svc.drain()
    assert client.calls[-1] == ("stop",)
    assert not pipe.is_open


async def test_arm_up_releases_speakers_after_timeout(make_service):
    svc, client, pipe, clock = make_service(TONEARM_QUIET_TIMEOUT_S="10")
    await start_by_needle_drop(svc, clock)
    feed(svc, clock, SILENT_CHUNK, 10.5)
    await svc.drain()
    assert svc.gate.state is State.ARMED
    assert client.calls[-1] == ("stop",)
    assert not pipe.is_open


async def test_start_retries_when_owntone_returns(make_service):
    svc, client, pipe, clock = make_service()
    client.fail = True
    svc.owntone_up = True
    await start_by_needle_drop(svc, clock)
    assert svc.snapshot()["error"] == "OwnTone down"
    assert svc.owntone_up is False
    assert not pipe.is_open
    client.fail = False
    clock.advance(6)
    await svc.tick()
    await svc.drain()
    assert ("play_uri", "library:track:1") in client.calls
    assert pipe.is_open
    assert svc.snapshot()["error"] is None


async def test_auto_retry_recomputes_speakers(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["1", "9"]))
    client.fail = True
    await start_by_needle_drop(svc, clock)
    client.fail = False
    await svc.refresh_outputs()
    clock.advance(6)
    await svc.tick()
    await svc.drain()
    set_outputs_calls = [c for c in client.calls if c[0] == "set_outputs"]
    assert set_outputs_calls[-1] == ("set_outputs", ["1"])


async def test_prebuffer_survives_slow_start(make_service):
    svc, client, pipe, clock = make_service()
    client.hold = asyncio.Event()
    feed(svc, clock, LOUD_CHUNK, 1.1)
    assert svc.gate.state is State.PLAYING
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pipe.is_open
    feed(svc, clock, LOUD_CHUNK, 1.5)
    client.hold.set()
    await svc.drain()
    assert pipe.is_open
    assert len(pipe.writes) == 52


async def test_play_uri_delay_does_not_lose_needle_drop(make_service):
    svc, client, pipe, clock = make_service()
    client.hold_play = asyncio.Event()
    prebuffer_chunks = distinct_loud_chunks(22, 3000)
    feed_sequence(svc, clock, prebuffer_chunks)
    assert svc.gate.state is State.PLAYING
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pipe.is_open
    assert ("play_uri", "library:track:1") in client.calls
    collected_chunks = distinct_loud_chunks(30, 4000)
    feed_sequence(svc, clock, collected_chunks)
    client.hold_play.set()
    await svc.drain()
    assert pipe.is_open
    assert pipe.writes == prebuffer_chunks + collected_chunks


async def test_reconnect_backoff_grows_while_notifications_keep_failing(make_service, monkeypatch):
    svc, client, pipe, clock = make_service()
    client.notify_fail = True
    delays = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 4:
            raise _StopLoop()
        await real_sleep(0)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        await svc._owntone_loop()
    assert delays == [1.0, 2.0, 4.0, 8.0]


async def test_close_stops_owntone_when_playing(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    await svc.close()
    assert ("stop",) in client.calls


async def test_startup_releases_leftover_pipe_playback(make_service):
    svc, client, pipe, clock = make_service()
    client.state = "play"
    client.now_path = svc.config.pipe_path
    await svc.start()
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert ("stop",) in client.calls
    finally:
        await svc.close()


async def test_startup_does_not_release_other_playback(make_service):
    svc, client, pipe, clock = make_service()
    client.state = "play"
    client.now_path = "library:track:999"
    await svc.start()
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert ("stop",) not in client.calls
    finally:
        await svc.close()


async def test_leftover_check_does_not_stop_fresh_playback(make_service):
    svc, client, pipe, clock = make_service()
    client.state = "play"
    client.now_path = svc.config.pipe_path
    gate_open = asyncio.Event()
    orig_player_state = client.player_state

    async def slow_player_state():
        await gate_open.wait()
        return await orig_player_state()

    client.player_state = slow_player_state
    loop_task = asyncio.create_task(svc._owntone_loop())
    for _ in range(5):
        await asyncio.sleep(0)
    feed(svc, clock, LOUD_CHUNK, 1.1)
    await svc.drain()
    assert ("play_uri", "library:track:1") in client.calls
    gate_open.set()
    for _ in range(10):
        await asyncio.sleep(0)
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    assert ("stop",) not in client.calls
    assert svc.gate.state is State.PLAYING


async def test_playback_resumes_after_owntone_restart_mid_record(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    loop_task = asyncio.create_task(svc._owntone_loop())
    for _ in range(5):
        await asyncio.sleep(0)
    assert svc.owntone_up
    client.fail = True
    await client.notify.put(None)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not svc.owntone_up
    client.fail = False
    clock.advance(6)
    await svc.tick()
    await svc.drain()
    plays = [c for c in client.calls if c[0] == "play_uri"]
    assert len(plays) == 2
    assert pipe.opens == 2
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await svc.close()


async def test_websocket_failure_does_not_restart_healthy_playback(make_service, monkeypatch):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    client.notify_fail = True
    client.now_path = svc.config.pipe_path
    real_sleep = asyncio.sleep
    iterations = []

    async def fake_sleep(seconds):
        iterations.append(seconds)
        if len(iterations) > 6:
            raise _StopLoop()
        feed(svc, clock, LOUD_CHUNK, 0.5)
        clock.advance(6)
        await svc.tick()
        await svc.drain()
        await real_sleep(0)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        await svc._owntone_loop()
    plays = [c for c in client.calls if c[0] == "play_uri"]
    assert len(plays) == 1, f"healthy playback restarted {len(plays) - 1} times"


async def test_unplugged_input_releases_speakers_after_timeout(make_service):
    svc, client, pipe, clock = make_service(TONEARM_QUIET_TIMEOUT_S="10")
    await start_by_needle_drop(svc, clock)
    svc.capture.on_available(False)
    for _ in range(12):
        clock.advance(1)
        await svc.tick()
    await svc.drain()
    assert svc.gate.state is State.ARMED
    assert client.calls[-1] == ("stop",)
    assert svc.snapshot()["input_available"] is False


async def test_stop_from_another_app_holds(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    client.state = "stop"
    clock.advance(5)
    await svc.handle_player_change()
    assert svc.gate.state is State.PLAYING
    clock.advance(15)
    await svc.handle_player_change()
    assert svc.gate.state is State.HELD
    assert not pipe.is_open


async def test_pin_while_playing_restarts_playback(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    client.calls.clear()
    await svc.set_speaker("3", pin="1234")
    assert client.calls[0] == ("update_output", "3", {"pin": "1234"})
    assert ("set_outputs", ["1", "2", "3"]) in client.calls
    assert client.calls[-1] == ("play_uri", "library:track:1")


async def test_volume_change_does_not_restart(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    client.calls.clear()
    await svc.set_speaker("1", volume=30)
    assert ("play_uri", "library:track:1") not in client.calls
    assert svc.snapshot()["speakers"][0]["volume"] == 30


async def test_play_with_mixed_available_and_unavailable_speaker_ids(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    await svc.play(["1", "9"])
    assert ("set_outputs", ["1"]) in client.calls


async def test_play_with_only_unavailable_speaker_id_reports_no_speakers_error(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.calls.clear()
    await svc.play(["9"])
    assert client.calls == []
    assert svc.snapshot()["error"] == service_module.NO_SPEAKERS_ERROR


async def test_play_with_available_and_gone_grace_speaker_ids(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    client.calls.clear()
    await svc.play(["1", "2"])
    assert ("set_outputs", ["1"]) in client.calls


async def test_manual_play_uses_given_speakers(make_service):
    svc, client, pipe, clock = make_service()
    await svc.play(["3"])
    assert svc.gate.state is State.PLAYING
    assert ("set_outputs", ["3"]) in client.calls


async def test_manual_play_defaults_to_default_set(make_service):
    svc, client, pipe, clock = make_service()
    await svc.play()
    assert ("set_outputs", ["1", "2"]) in client.calls


async def test_manual_play_prefers_current_selection(make_service):
    svc, client, pipe, clock = make_service()
    await client.set_outputs(["2"])
    await svc.refresh_outputs()
    client.calls.clear()
    await svc.play()
    assert ("set_outputs", ["2"]) in client.calls


async def test_play_while_playing_is_noop(make_service):
    svc, client, pipe, clock = make_service()
    await svc.play()
    count = len(client.calls)
    await svc.play(["3"])
    assert len(client.calls) == count


async def test_pipe_missing_from_library_is_reported(make_service):
    svc, client, pipe, clock = make_service()
    client.track_uri = None
    await svc.play()
    assert "not in the OwnTone library" in svc.snapshot()["error"]
    assert not pipe.is_open


async def test_auto_on_switch_persists(make_service, tmp_path):
    svc, client, pipe, clock = make_service()
    svc.set_auto_on(False)
    assert svc.gate.state is State.OFF
    assert load(tmp_path / "settings.json").auto_on is False


async def test_default_speakers_persist(make_service, tmp_path):
    svc, client, pipe, clock = make_service()
    svc.set_default_speakers(["3"])
    assert load(tmp_path / "settings.json").default_speakers == ["3"]


async def test_settings_save_failure_is_reported(make_service, tmp_path):
    (tmp_path / "blocker").write_text("not a directory")
    svc, client, pipe, clock = make_service(TONEARM_SETTINGS=str(tmp_path / "blocker" / "settings.json"))
    svc.set_auto_on(False)
    assert "settings" in svc.snapshot()["error"].lower()
    assert svc.gate.state is State.OFF


async def test_set_input_restarts_capture_and_persists(make_service, tmp_path):
    svc, client, pipe, clock = make_service()
    await svc.set_input("plughw:0,0")
    assert svc.capture.stops == 1
    assert svc.capture.starts[-1] == "plughw:0,0"
    assert load(tmp_path / "settings.json").input == "plughw:0,0"


async def test_start_migrates_legacy_input_id_when_device_listed(make_service, tmp_path):
    async def list_inputs():
        return [{"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:3,0"}]

    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:3,0", default_speakers=["1"]),
                                            list_inputs=list_inputs)
    await svc.start()
    try:
        assert svc.settings.input == "plughw:CARD=CODEC,DEV=0"
        assert svc.capture.starts == ["plughw:CARD=CODEC,DEV=0"]
        assert load(tmp_path / "settings.json").input == "plughw:CARD=CODEC,DEV=0"
    finally:
        await svc.close()


async def test_start_leaves_legacy_input_id_when_device_not_listed(make_service):
    async def list_inputs():
        return [{"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:9,0"}]

    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:3,0", default_speakers=["1"]),
                                            list_inputs=list_inputs)
    await svc.start()
    try:
        assert svc.settings.input == "plughw:3,0"
        assert svc.capture.starts == ["plughw:3,0"]
    finally:
        await svc.close()


async def test_start_leaves_name_form_input_unchanged(make_service):
    async def list_inputs():
        return [{"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:3,0"}]

    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:CARD=CODEC,DEV=0", default_speakers=["1"]),
                                            list_inputs=list_inputs)
    await svc.start()
    try:
        assert svc.settings.input == "plughw:CARD=CODEC,DEV=0"
    finally:
        await svc.close()


async def test_list_inputs_hides_legacy_id(make_service):
    async def list_inputs():
        return [{"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:3,0"}]

    svc, client, pipe, clock = make_service(list_inputs=list_inputs)
    assert await svc.list_inputs() == [{"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC"}]


async def test_list_inputs_migrates_legacy_id_once_the_device_appears(make_service, tmp_path, caplog):
    devices = []

    async def list_inputs():
        return list(devices)

    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:3,0", default_speakers=["1"]),
                                            list_inputs=list_inputs)
    await svc.start()
    try:
        assert svc.settings.input == "plughw:3,0"

        devices.append({"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:3,0"})
        with caplog.at_level(logging.INFO, logger="tonearm.service"):
            result = await svc.list_inputs()
        assert svc.settings.input == "plughw:CARD=CODEC,DEV=0"
        assert load(tmp_path / "settings.json").input == "plughw:CARD=CODEC,DEV=0"
        assert result == [{"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC"}]
        assert any(r.message == "migrated input plughw:3,0 -> plughw:CARD=CODEC,DEV=0" for r in caplog.records)
    finally:
        await svc.close()


async def test_input_available_migrates_legacy_id_once_the_device_appears(make_service, tmp_path):
    devices = []

    async def list_inputs():
        return list(devices)

    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:3,0", default_speakers=["1"]),
                                            list_inputs=list_inputs)
    await svc.start()
    try:
        devices.append({"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:3,0"})
        svc.capture.on_available(True)
        await svc.drain()
        assert svc.settings.input == "plughw:CARD=CODEC,DEV=0"
        assert load(tmp_path / "settings.json").input == "plughw:CARD=CODEC,DEV=0"
    finally:
        await svc.close()


async def test_start_picks_last_input_when_none_configured(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input=None, default_speakers=["1"]))
    await svc.start()
    try:
        assert svc.settings.input == "plughw:1,0"
        assert svc.capture.starts == ["plughw:1,0"]
    finally:
        await svc.close()


async def test_snapshot_reports_speakers_and_missing_defaults(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["1", "9"]))
    assert svc.snapshot()["missing_default_speakers"] == []
    await svc.refresh_outputs()
    snap = svc.snapshot()
    assert snap["state"] == "armed"
    assert snap["auto_on"] is True
    assert snap["countdown_s"] is None
    assert [s["id"] for s in snap["speakers"]] == ["1", "2", "3"]
    assert snap["speakers"][0]["is_default"] is True
    assert snap["speakers"][2]["needs_pin"] is True
    assert snap["missing_default_speakers"] == ["9"]


async def test_countdown_in_snapshot(make_service):
    svc, client, pipe, clock = make_service(TONEARM_QUIET_TIMEOUT_S="10")
    await start_by_needle_drop(svc, clock)
    feed(svc, clock, SILENT_CHUNK, 3.05)
    assert svc.snapshot()["countdown_s"] == 7


async def test_subscribers_receive_snapshots(make_service):
    svc, client, pipe, clock = make_service()
    queue = svc.subscribe()
    await svc.play()
    assert (await queue.get())["state"] == "playing"
    svc.unsubscribe(queue)


async def test_retries_do_not_pile_up_while_start_is_running(make_service):
    svc, client, pipe, clock = make_service()
    client.fail = True
    await start_by_needle_drop(svc, clock)
    client.fail = False
    client.hold = asyncio.Event()
    clock.advance(6)
    await svc.tick()
    for _ in range(5):
        await asyncio.sleep(0)
    for _ in range(10):
        clock.advance(1)
        await svc.tick()
    client.hold.set()
    await svc.drain()
    assert sum(1 for c in client.calls if c[0] == "play_uri") == 1
    assert pipe.opens == 1


async def test_own_restart_is_not_mistaken_for_external_stop(make_service):
    svc, client, pipe, clock = make_service()
    await start_by_needle_drop(svc, clock)
    clock.advance(20)
    client.state = "stop"
    client.hold = asyncio.Event()
    task = asyncio.ensure_future(svc.set_speaker("3", pin="1234"))
    for _ in range(5):
        await asyncio.sleep(0)
    await svc.handle_player_change()
    assert svc.gate.state is State.PLAYING
    client.hold.set()
    await task
    await svc.drain()
    assert svc.gate.state is State.PLAYING
    assert client.calls[-1] == ("play_uri", "library:track:1")


async def test_cancelled_play_does_not_leak_collector(make_service):
    svc, client, pipe, clock = make_service()
    svc.set_auto_on(False)
    client.hold = asyncio.Event()
    task = asyncio.create_task(svc.play(["1"]))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    feed(svc, clock, LOUD_CHUNK, 10.0)
    assert not svc._collecting
    await svc.stop()


async def test_unexpected_error_in_start_is_reported_and_retried(make_service):
    svc, client, pipe, clock = make_service()
    client.find_error = AttributeError("boom")
    await start_by_needle_drop(svc, clock)
    assert "AttributeError" in svc.snapshot()["error"]
    assert not pipe.is_open
    client.find_error = None
    clock.advance(6)
    await svc.tick()
    await svc.drain()
    assert ("play_uri", "library:track:1") in client.calls
    assert pipe.is_open


async def test_new_speaker_hidden_until_appear_delay(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.outputs_list.append(Output("4", "Ben's MacBook Air", "AirPlay 2", False, 50, False))
    await svc.refresh_outputs()
    assert "4" not in [s["id"] for s in svc.snapshot()["speakers"]]
    clock.advance(9.9)
    await svc.tick()
    assert "4" not in [s["id"] for s in svc.snapshot()["speakers"]]
    clock.advance(0.2)
    await svc.tick()
    assert "4" in [s["id"] for s in svc.snapshot()["speakers"]]


async def test_disappeared_speaker_kept_with_grace_then_dropped(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    snap = svc.snapshot()
    den = next(s for s in snap["speakers"] if s["id"] == "2")
    assert den["available"] is False
    assert den["selected"] is False
    assert den["needs_pin"] is False
    assert den["name"] == "Den"
    clock.advance(29.9)
    await svc.tick()
    assert any(s["id"] == "2" for s in svc.snapshot()["speakers"])
    clock.advance(0.2)
    await svc.tick()
    assert not any(s["id"] == "2" for s in svc.snapshot()["speakers"])


async def test_speaker_reappearing_within_grace_is_immediately_available(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    original = next(o for o in client.outputs_list if o.id == "2")
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    clock.advance(5)
    client.outputs_list.append(original)
    await svc.refresh_outputs()
    den = next(s for s in svc.snapshot()["speakers"] if s["id"] == "2")
    assert den["available"] is True


async def test_auto_speakers_skips_unavailable(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["1", "2"]))
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    assert svc._auto_speakers() == ["1"]


async def test_set_speaker_on_unavailable_id_raises(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    client.calls.clear()
    with pytest.raises(OwnToneError, match="is no longer available"):
        await svc.set_speaker("2", volume=10)
    assert client.calls == []


async def test_set_speaker_owntone_rejection_for_vanished_output_is_friendly(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()

    async def vanish_and_fail(output_id, *, selected=None, volume=None, pin=None):
        client.outputs_list = [o for o in client.outputs_list if o.id != output_id]
        raise OwnToneError(f"PUT /api/outputs/{output_id} returned HTTP 400")

    client.update_output = vanish_and_fail
    with pytest.raises(OwnToneError, match="is no longer available"):
        await svc.set_speaker("1", pin="1234")


async def test_selecting_unpaired_speaker_rejection_asks_for_pin(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()

    async def need_pin_and_fail(output_id, *, selected=None, volume=None, pin=None):
        client.outputs_list = [Output(o.id, o.name, o.type, o.selected, o.volume, o.id == output_id)
                               for o in client.outputs_list]
        raise OwnToneError(f"PUT /api/outputs/{output_id} returned HTTP 400")

    client.update_output = need_pin_and_fail
    name = next(o.name for o in client.outputs_list if o.id == "1")
    with pytest.raises(OwnToneError, match=f"^{name} needs pairing"):
        await svc.set_speaker("1", selected=True)


async def test_manual_play_excludes_speaker_in_grace(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["1", "2"]))
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    client.calls.clear()
    await svc.play()
    assert ("set_outputs", ["1"]) in client.calls


async def test_manual_play_with_nothing_available_reports_no_speakers_error(make_service):
    svc, client, pipe, clock = make_service(settings=Settings(input="plughw:1,0", default_speakers=["1"]))
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "1"]
    await svc.refresh_outputs()
    client.calls.clear()
    await svc.play()
    assert client.calls == []
    assert svc.snapshot()["error"] == service_module.NO_SPEAKERS_ERROR


async def test_manual_resume_excludes_vanished_speaker(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.fail = True
    await svc.play(["1", "2"])
    assert svc._pending_start == ["1", "2"]
    assert svc._pending_auto is False
    client.fail = False
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    clock.advance(6)
    client.calls.clear()
    await svc.tick()
    await svc.drain()
    set_outputs_calls = [c for c in client.calls if c[0] == "set_outputs"]
    assert set_outputs_calls[-1] == ("set_outputs", ["1"])


async def test_pin_restart_excludes_not_yet_available_speaker(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    await start_by_needle_drop(svc, clock)
    # A speaker OwnTone already reports selected, but that has only just appeared
    # and is still within its own appear delay (hidden/"pending"), must not be
    # swept into the pin-restart's speaker list.
    client.outputs_list.append(Output("4", "Ben's MacBook Air", "AirPlay 2", True, 50, False))
    await svc.refresh_outputs()
    client.calls.clear()
    await svc.set_speaker("3", pin="1234")
    assert ("set_outputs", ["1", "2", "3"]) in client.calls


async def test_flapping_speaker_never_shown(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    flapper = Output("5", "Ben's MacBook Air", "AirPlay 2", False, 50, False)
    for _ in range(3):
        client.outputs_list.append(flapper)
        await svc.refresh_outputs()
        assert "5" not in [s["id"] for s in svc.snapshot()["speakers"]]
        clock.advance(5.0)
        await svc.tick()
        assert "5" not in [s["id"] for s in svc.snapshot()["speakers"]]
        client.outputs_list = [o for o in client.outputs_list if o.id != "5"]
        await svc.refresh_outputs()
        assert "5" not in [s["id"] for s in svc.snapshot()["speakers"]]
        clock.advance(5.0)
        await svc.tick()
        assert "5" not in [s["id"] for s in svc.snapshot()["speakers"]]


async def test_speaker_shown_at_exactly_appear_delay_boundary(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.outputs_list.append(Output("4", "Ben's MacBook Air", "AirPlay 2", False, 50, False))
    await svc.refresh_outputs()
    clock.advance(service_module.SPEAKER_APPEAR_DELAY_S)
    await svc.tick()
    assert "4" in [s["id"] for s in svc.snapshot()["speakers"]]


async def test_speaker_dropped_at_exactly_grace_boundary(make_service):
    svc, client, pipe, clock = make_service()
    await svc.refresh_outputs()
    client.outputs_list = [o for o in client.outputs_list if o.id != "2"]
    await svc.refresh_outputs()
    clock.advance(service_module.SPEAKER_GONE_GRACE_S)
    await svc.tick()
    assert not any(s["id"] == "2" for s in svc.snapshot()["speakers"])
