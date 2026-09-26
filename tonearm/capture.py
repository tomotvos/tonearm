import asyncio
import collections
import contextlib
import logging
import re
from typing import Callable, Sequence

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
CHUNK_BYTES = SAMPLE_RATE * CHANNELS * 2 // 20

_CARD_LINE = re.compile(r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+):")


def arecord_command(device: str) -> list[str]:
    return ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS), "-t", "raw"]


def parse_arecord_list(text: str) -> list[dict]:
    inputs = []
    for line in text.splitlines():
        match = _CARD_LINE.match(line)
        if match:
            card, card_id, name, device = match.groups()
            inputs.append({
                "id": f"plughw:CARD={card_id},DEV={device}",
                "name": name.strip(),
                "legacy_id": f"plughw:{card},{device}",
            })
    return inputs


async def list_inputs() -> list[dict]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-l", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return []
    out, _ = await proc.communicate()
    return parse_arecord_list(out.decode(errors="replace"))


class Capture:
    def __init__(self, on_chunk: Callable[[bytes], None], on_available: Callable[[bool], None],
                 command: Callable[[str], list[str]] = arecord_command,
                 backoff_s: Sequence[float] = (1, 2, 5, 10, 30)):
        self._on_chunk = on_chunk
        self._on_available = on_available
        self._command = command
        self._backoff = list(backoff_s)
        self._available = None
        self._task = None

    def start(self, device: str) -> None:
        if self._task is not None:
            raise RuntimeError("capture already running")
        self._task = asyncio.get_running_loop().create_task(self._run(device))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _set_available(self, available: bool) -> None:
        if available != self._available:
            self._available = available
            self._on_available(available)

    async def _run(self, device: str) -> None:
        attempt = 0
        while True:
            if await self._capture_once(device):
                attempt = 0
            self._set_available(False)
            await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            attempt += 1

    async def _capture_once(self, device: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command(device), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            log.warning("cannot start capture on %s: %s", device, e)
            return False

        stderr_lines = collections.deque(maxlen=20)
        stderr_task = asyncio.ensure_future(self._drain_stderr(proc, stderr_lines))
        received = False
        try:
            while True:
                chunk = await proc.stdout.readexactly(CHUNK_BYTES)
                received = True
                self._set_available(True)
                self._on_chunk(chunk)
        except asyncio.IncompleteReadError:
            pass
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

        err_tail = "".join(stderr_lines)[-300:] if stderr_lines else ""
        log.warning("capture on %s exited with code %s: %s", device, proc.returncode, err_tail)
        return received

    async def _drain_stderr(self, proc, stderr_lines: collections.deque) -> None:
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_lines.append(chunk.decode(errors="replace"))
        except asyncio.CancelledError:
            pass
