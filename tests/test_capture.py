import asyncio
import sys
import time

from tonearm.capture import CHUNK_BYTES, Capture, arecord_command, parse_arecord_list

ARECORD_LIST = """**** List of CAPTURE Hardware Devices ****
card 0: I82801BAICH2 [Intel 82801BA-ICH2], device 0: Intel ICH [Intel 82801BA-ICH2]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 0: I82801BAICH2 [Intel 82801BA-ICH2], device 1: Intel ICH - MIC ADC [Intel 82801BA-ICH2 - MIC ADC]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: CODEC [USB AUDIO  CODEC], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def test_arecord_command():
    assert arecord_command("plughw:1,0") == [
        "arecord", "-q", "-D", "plughw:1,0", "-f", "S16_LE", "-r", "44100", "-c", "2", "-t", "raw",
    ]


def test_parse_arecord_list():
    assert parse_arecord_list(ARECORD_LIST) == [
        {"id": "plughw:CARD=I82801BAICH2,DEV=0", "name": "Intel 82801BA-ICH2", "legacy_id": "plughw:0,0"},
        {"id": "plughw:CARD=I82801BAICH2,DEV=1", "name": "Intel 82801BA-ICH2", "legacy_id": "plughw:0,1"},
        {"id": "plughw:CARD=CODEC,DEV=0", "name": "USB AUDIO  CODEC", "legacy_id": "plughw:1,0"},
    ]


def test_parse_arecord_list_card_id_with_digits_and_underscore():
    text = "card 1: Device_1 [USB Audio Device], device 0: USB Audio [USB Audio]\n"
    assert parse_arecord_list(text) == [
        {"id": "plughw:CARD=Device_1,DEV=0", "name": "USB Audio Device", "legacy_id": "plughw:1,0"},
    ]


def test_parse_empty_list():
    assert parse_arecord_list("") == []


def writer_command(n_bytes):
    code = f"import sys; sys.stdout.buffer.write(b'\\x01' * {n_bytes})"
    return lambda device: [sys.executable, "-c", code]


async def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


async def test_delivers_whole_chunks_and_reports_availability():
    chunks, availability = [], []
    capture = Capture(chunks.append, availability.append,
                      command=writer_command(CHUNK_BYTES * 2 + 100), backoff_s=(60,))
    capture.start("dev")
    await wait_for(lambda: availability == [True, False])
    await capture.stop()
    assert len(chunks) == 2
    assert all(len(c) == CHUNK_BYTES for c in chunks)


async def test_restarts_after_exit():
    launches = []

    def command(device):
        launches.append(device)
        return [sys.executable, "-c", "pass"]

    capture = Capture(lambda c: None, lambda a: None, command=command, backoff_s=(0.01,))
    capture.start("plughw:1,0")
    await wait_for(lambda: len(launches) >= 3)
    await capture.stop()
    assert set(launches) == {"plughw:1,0"}


async def test_missing_program_reports_unavailable():
    availability = []
    capture = Capture(lambda c: None, availability.append,
                      command=lambda d: ["/nonexistent/arecord"], backoff_s=(60,))
    capture.start("dev")
    await wait_for(lambda: availability == [False])
    await capture.stop()


async def test_stop_ends_a_running_capture_promptly():
    capture = Capture(lambda c: None, lambda a: None,
                      command=lambda d: [sys.executable, "-c", "import time; time.sleep(30)"])
    capture.start("dev")
    await asyncio.sleep(0.2)
    started = time.monotonic()
    await capture.stop()
    assert time.monotonic() - started < 2.0


async def test_start_twice_is_an_error():
    capture = Capture(lambda c: None, lambda a: None,
                      command=lambda d: [sys.executable, "-c", "import time; time.sleep(30)"])
    capture.start("dev")
    try:
        try:
            capture.start("dev")
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
    finally:
        await capture.stop()


async def test_heavy_stderr_does_not_stall_capture():
    chunks, availability = [], []
    code = f"import sys; sys.stderr.write('x' * 1_000_000); sys.stderr.flush(); sys.stdout.buffer.write(b'\\x01' * {CHUNK_BYTES * 2}); sys.stdout.flush()"
    capture = Capture(chunks.append, availability.append,
                      command=lambda d: [sys.executable, "-c", code], backoff_s=(60,))
    capture.start("dev")
    await wait_for(lambda: len(chunks) == 2 and availability == [True, False])
    await capture.stop()
