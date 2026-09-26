import logging
import os
import threading
import time

from tonearm.pipe import PipeWriter


def make_fifo(tmp_path):
    path = str(tmp_path / "pipe")
    os.mkfifo(path)
    return path


def drain(fd, out):
    while chunk := os.read(fd, 65536):
        out.extend(chunk)


def test_round_trip(tmp_path):
    path = make_fifo(tmp_path)
    rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = PipeWriter(path, retry_s=0.01)
    writer.open()
    assert writer.connected.wait(2)
    os.set_blocking(rfd, True)
    received = bytearray()
    reader = threading.Thread(target=drain, args=(rfd, received))
    reader.start()
    for i in range(10):
        writer.write(bytes([i]) * 1000)
    writer.close()
    reader.join(2)
    os.close(rfd)
    assert bytes(received) == b"".join(bytes([i]) * 1000 for i in range(10))


def test_no_reader_drops_instead_of_blocking(tmp_path):
    path = make_fifo(tmp_path)
    writer = PipeWriter(path, max_chunks=100, retry_s=0.01)
    writer.open()
    for _ in range(150):
        writer.write(b"x" * 100)
    assert writer.dropped == 50
    started = time.monotonic()
    writer.close()
    assert time.monotonic() - started < 1.0
    assert not writer.is_open


def test_dropped_chunks_are_logged_once_per_burst(tmp_path, caplog):
    path = make_fifo(tmp_path)
    writer = PipeWriter(path, max_chunks=10, retry_s=0.01)
    writer.open()
    with caplog.at_level(logging.WARNING, logger="tonearm.pipe"):
        for _ in range(20):
            writer.write(b"x" * 100)
    dropped_records = [r for r in caplog.records if "dropped" in r.message.lower()]
    assert len(dropped_records) == 1
    writer.close()


def test_default_max_chunks_is_200(tmp_path):
    writer = PipeWriter(make_fifo(tmp_path))
    assert writer._max_chunks == 200


def test_connect_and_drain_timeline_is_logged(tmp_path, caplog):
    path = make_fifo(tmp_path)
    rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = PipeWriter(path, retry_s=0.01)
    with caplog.at_level(logging.INFO, logger="tonearm.pipe"):
        writer.open()
        assert writer.connected.wait(2)
        os.set_blocking(rfd, True)
        received = bytearray()
        reader = threading.Thread(target=drain, args=(rfd, received))
        reader.start()
        writer.write(b"x" * 100)
        time.sleep(0.1)
        writer.close()
        reader.join(2)
    os.close(rfd)
    messages = [r.message for r in caplog.records]
    assert any(m.startswith("pipe reader connected after") for m in messages)
    assert any(m.startswith("pipe first write completed after") for m in messages)
    assert any(m.startswith("pipe backlog drained after") for m in messages)


def test_write_when_closed_is_ignored(tmp_path):
    writer = PipeWriter(make_fifo(tmp_path))
    writer.write(b"x")
    assert writer.dropped == 0
    assert not writer.is_open


def test_writer_reconnects_after_reader_goes_away(tmp_path):
    path = make_fifo(tmp_path)
    rfd1 = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = PipeWriter(path, retry_s=0.01)
    writer.open()
    assert writer.connected.wait(2)
    os.close(rfd1)
    for _ in range(3):
        writer.write(b"x" * 1000)
    started = time.monotonic()
    while writer.connected.is_set() and time.monotonic() - started < 2.0:
        time.sleep(0.01)
    assert not writer.connected.is_set()
    rfd2 = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    assert writer.connected.wait(2)
    os.set_blocking(rfd2, True)
    received = bytearray()
    reader = threading.Thread(target=drain, args=(rfd2, received))
    reader.start()
    marker = b"y" * 1000
    writer.write(marker)
    writer.close()
    reader.join(2)
    os.close(rfd2)
    assert marker in bytes(received)
    assert not writer.is_open


def test_reader_disconnect_is_handled(tmp_path):
    path = make_fifo(tmp_path)
    rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = PipeWriter(path, retry_s=0.01)
    writer.open()
    assert writer.connected.wait(2)
    os.close(rfd)
    for _ in range(5):
        writer.write(b"x" * 1000)
    writer.close()
    assert not writer.is_open


def test_open_is_idempotent_and_reopenable(tmp_path):
    path = make_fifo(tmp_path)
    writer = PipeWriter(path, retry_s=0.01)
    writer.open()
    writer.open()
    writer.close()
    writer.open()
    assert writer.is_open
    writer.close()


def test_close_aborts_a_stalled_reader(tmp_path):
    path = make_fifo(tmp_path)
    rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = PipeWriter(path, max_chunks=200, retry_s=0.01)
    writer.open()
    assert writer.connected.wait(2)
    for _ in range(150):
        writer.write(b"x" * 8820)
    thread = writer._thread
    started = time.monotonic()
    writer.close(timeout=0.3)
    assert time.monotonic() - started < 2.0
    assert not thread.is_alive()
    writer.open()
    assert writer.is_open
    writer.close()
    os.close(rfd)
