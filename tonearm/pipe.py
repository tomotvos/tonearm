import errno
import logging
import os
import queue
import select
import threading
import time

log = logging.getLogger(__name__)

DROP_LOG_INTERVAL_S = 10.0


class PipeWriter:
    def __init__(self, path: str, max_chunks: int = 200, retry_s: float = 0.1):
        self._path = path
        self._max_chunks = max_chunks
        self._retry_s = retry_s
        self._thread = None
        self._queue = None
        self._stop = None
        self._abort = None
        self.dropped = 0
        self._last_drop_log = None
        self._opened_at = None
        self.connected = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._thread is not None

    def open(self) -> None:
        if self._thread is not None:
            return
        self._queue = queue.Queue(maxsize=self._max_chunks)
        self._stop = threading.Event()
        self._abort = threading.Event()
        self.connected = threading.Event()
        self.dropped = 0
        self._last_drop_log = None
        self._opened_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, args=(self._queue, self._stop, self._abort, self.connected), daemon=True, name="tonearm-pipe-writer")
        self._thread.start()

    def write(self, chunk: bytes) -> None:
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            self.dropped += 1
            now = time.monotonic()
            if self._last_drop_log is None or now - self._last_drop_log >= DROP_LOG_INTERVAL_S:
                log.warning("dropped %d chunk(s) writing to pipe (queue full)", self.dropped)
                self._last_drop_log = now

    def close(self, timeout: float = 2.0) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
        thread.join(timeout)
        if thread.is_alive():
            self._abort.set()
            thread.join(1.0)
        if thread.is_alive():
            log.warning("pipe writer did not finish within %.1f s", timeout)
        if self.dropped:
            log.warning("dropped %d chunk(s) total while pipe was open", self.dropped)

    def _connect(self, stop: threading.Event) -> int | None:
        while not stop.is_set():
            try:
                return os.open(self._path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno != errno.ENXIO:
                    log.error("cannot open pipe %s: %s", self._path, e)
                    return None
                stop.wait(self._retry_s)
        return None

    def _run(self, chunks: queue.Queue, stop: threading.Event, abort: threading.Event, connected: threading.Event) -> None:
        fd = None
        while not stop.is_set():
            try:
                fd = self._connect(stop)
                if fd is None:
                    return
                connect_time = time.monotonic()
                log.info("pipe reader connected after %.0f ms", (connect_time - self._opened_at) * 1000)
                connected.set()
                first_write_logged = False
                drained_logged = False
                while (chunk := chunks.get()) is not None:
                    view = memoryview(chunk)
                    while view:
                        if abort.is_set():
                            return
                        _, writable, _ = select.select([], [fd], [], 0.1)
                        if writable:
                            try:
                                n = os.write(fd, view)
                                view = view[n:]
                            except BlockingIOError:
                                continue
                    if not first_write_logged:
                        log.info("pipe first write completed after %.0f ms", (time.monotonic() - connect_time) * 1000)
                        first_write_logged = True
                    if not drained_logged and chunks.qsize() == 0:
                        log.info("pipe backlog drained after %.0f ms", (time.monotonic() - connect_time) * 1000)
                        drained_logged = True
                return
            except BrokenPipeError:
                log.info("pipe reader went away; waiting to reconnect")
                connected.clear()
            except OSError as e:
                log.error("pipe write failed: %s", e)
                return
            finally:
                if fd is not None:
                    os.close(fd)
                    fd = None
