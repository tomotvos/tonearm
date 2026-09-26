import asyncio
import dataclasses
import logging
import math
import re
import time
from collections import deque

from .capture import list_inputs as default_list_inputs
from .gate import Action, Gate, GateConfig, State
from .levels import SILENCE_DB, rms_dbfs
from .owntone import OwnToneError
from .settings import save

log = logging.getLogger(__name__)

PREBUFFER_CHUNKS = 40
RETRY_INTERVAL_S = 5.0
EXTERNAL_STOP_GRACE_S = 15.0
LEVEL_BROADCAST_INTERVAL_S = 0.2
SPEAKER_APPEAR_DELAY_S = 10.0
SPEAKER_GONE_GRACE_S = 30.0
LEGACY_INPUT_ID_RE = re.compile(r"^plughw:\d+,\d+$")
NO_SPEAKERS_ERROR = "No speakers to play to: set default speakers, or select speakers and press Play"
AUTO_WAITING_FOR_SPEAKER_ERROR = ("None of your default speakers are available — Tonearm will start as soon as "
                                  "one is, or star another speaker")


@dataclasses.dataclass
class _Presence:
    output: object
    state: str  # "pending" (hidden, newly seen), "available", "gone" (grace)
    since: float


class Service:
    def __init__(self, config, client, pipe, settings, capture_factory,
                 list_inputs=default_list_inputs, clock=time.monotonic):
        self.config = config
        self.client = client
        self.pipe = pipe
        self.settings = settings
        self._list_inputs = list_inputs
        self._clock = clock
        self.gate = Gate(GateConfig(config.start_db, config.floor_db, config.quiet_timeout_s),
                         auto_on=settings.auto_on, now=clock())
        self.capture = capture_factory(self._on_chunk, self._on_input_available)
        self.outputs = []
        self._presence = {}
        self._first_contact_done = False
        self.level_db = SILENCE_DB
        self.input_available = False
        self.owntone_up = False
        self.last_error = None
        self._prebuffer = deque(maxlen=PREBUFFER_CHUNKS)
        self._collecting = None
        self._pipe_uri = None
        self._pending_start = None
        self._pending_auto = False
        self._playing_speakers = None
        self._playing_auto = False
        self._checked_leftover_playback = False
        self._last_start_attempt = -math.inf
        self._started_at = -math.inf
        self._lock = asyncio.Lock()
        self._subscribers = set()
        self._last_level_broadcast = -math.inf
        self._background = set()
        self._loops = []

    async def start(self):
        inputs = await self._migrate_legacy_input()
        if not self.settings.input:
            if inputs is None:
                inputs = await self._list_inputs()
            if inputs:
                self.settings.input = inputs[-1]["id"]
                self._save_settings()
        if self.settings.input:
            self.capture.start(self.settings.input)
        self._loops = [asyncio.create_task(self._owntone_loop()), asyncio.create_task(self._tick_loop())]

    async def close(self):
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        await self.drain()
        await self.capture.stop()
        if self.gate.state is State.PLAYING:
            try:
                await self.client.stop()
            except OwnToneError as e:
                log.warning("could not stop OwnTone on close: %s", e)
            except Exception:
                log.exception("unexpected error stopping OwnTone on close")
        await asyncio.to_thread(self.pipe.close)

    async def drain(self):
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def _on_chunk(self, chunk):
        self._prebuffer.append(chunk)
        if self._collecting is not None:
            self._collecting.append(chunk)
        self.level_db = rms_dbfs(chunk)
        if self.pipe.is_open:
            self.pipe.write(chunk)
        now = self._clock()
        self._apply(self.gate.feed(self.level_db, now))
        if now - self._last_level_broadcast >= LEVEL_BROADCAST_INTERVAL_S:
            self._broadcast()

    def _on_input_available(self, available):
        self.input_available = available
        if not available:
            self.level_db = SILENCE_DB
        elif self.settings.input and LEGACY_INPUT_ID_RE.match(self.settings.input):
            self._spawn(self._migrate_legacy_input())
        self._broadcast()

    async def _migrate_legacy_input(self, inputs=None):
        if not (self.settings.input and LEGACY_INPUT_ID_RE.match(self.settings.input)):
            return inputs
        if inputs is None:
            inputs = await self._list_inputs()
        match = next((i for i in inputs if i.get("legacy_id") == self.settings.input), None)
        if match:
            old = self.settings.input
            self.settings.input = match["id"]
            self._save_settings()
            log.info("migrated input %s -> %s", old, match["id"])
        return inputs

    async def tick(self):
        now = self._clock()
        self._evaluate_presence(now)
        if self.input_available:
            actions = self.gate.tick(now)
        else:
            actions = self.gate.feed(SILENCE_DB, now)
        self._apply(actions)
        if (self._pending_start is not None and self.gate.state is State.PLAYING
                and not self._lock.locked()
                and now - self._last_start_attempt >= RETRY_INTERVAL_S):
            self._last_start_attempt = now
            speakers = self._auto_speakers() if self._pending_auto else self._filter_available(self._pending_start)
            self._spawn(self._start_playback(speakers, retry=True, auto=self._pending_auto))
        self._broadcast()

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.tick()
            except Exception:
                log.exception("error during tick")

    def _apply(self, actions):
        for action in actions:
            if action is Action.START_AUTO:
                prebuffer = list(self._prebuffer)
                self._collecting = []
                speakers = self._auto_speakers()
                decided_at = self._clock()
                log.info("start decided: auto=True speakers=%s gate_state=%s prebuffer_chunks=%d",
                         speakers, self.gate.state.value, len(prebuffer))
                self._spawn(self._start_playback(speakers, auto=True, prebuffer=prebuffer, decided_at=decided_at))
            elif action is Action.STOP:
                self._spawn(self._stop_playback())

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._on_background_done)

    def _on_background_done(self, task):
        self._background.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("background task failed: %r", exc, exc_info=exc)
            self.last_error = f"Internal error: {exc!r}"
            self._broadcast()

    def _available_ids(self):
        return {oid for oid, p in self._presence.items() if p.state == "available"}

    def _filter_available(self, ids):
        if not self._presence:
            return list(ids)
        available = self._available_ids()
        return [i for i in ids if i in available]

    def _auto_speakers(self):
        return self._filter_available(self.settings.default_speakers)

    async def _start_playback(self, speaker_ids, *, retry=False, auto=False, prebuffer=None, decided_at=None):
        if decided_at is None:
            decided_at = self._clock()
        try:
            async with self._lock:
                if self.gate.state is not State.PLAYING:
                    return
                if retry and self._pending_start is None:
                    return
                self._last_start_attempt = self._clock()
                self._started_at = self._clock()
                await asyncio.to_thread(self.pipe.close)
                if not speaker_ids:
                    self._pending_start = [] if auto else None
                    self._pending_auto = auto
                    self.last_error = AUTO_WAITING_FOR_SPEAKER_ERROR if auto else NO_SPEAKERS_ERROR
                    self._broadcast()
                    return
                if prebuffer is None:
                    prebuffer = list(self._prebuffer)
                try:
                    if self._pipe_uri is None:
                        self._pipe_uri = await self.client.find_track_uri(self.config.pipe_path)
                        if self._pipe_uri is None:
                            raise OwnToneError(f"{self.config.pipe_path} is not in the OwnTone library")
                    await self.client.set_outputs(speaker_ids)
                    log.info("owntone accepted set_outputs after %.0f ms", (self._clock() - decided_at) * 1000)
                    await self.client.play_uri(self._pipe_uri)
                    log.info("owntone accepted play_uri after %.0f ms", (self._clock() - decided_at) * 1000)
                except OwnToneError as e:
                    log.warning("could not start playback: %s", e)
                    self._pending_start = list(speaker_ids)
                    self._pending_auto = auto
                    self.last_error = str(e)
                    self.owntone_up = False
                    self._broadcast()
                    return
                except Exception as e:
                    log.exception("unexpected error starting playback")
                    self._pending_start = list(speaker_ids)
                    self._pending_auto = auto
                    self.last_error = f"Unexpected error starting playback: {e!r}"
                    self._broadcast()
                    return
                collected, self._collecting = self._collecting, None
                self._pending_start = None
                self._pending_auto = False
                self._playing_speakers = list(speaker_ids)
                self._playing_auto = auto
                self._started_at = self._clock()
                self.last_error = None
                self.pipe.open()
                for chunk in prebuffer:
                    self.pipe.write(chunk)
                for chunk in collected or []:
                    self.pipe.write(chunk)
                self._broadcast()
        finally:
            self._collecting = None

    async def _stop_playback(self):
        async with self._lock:
            self._collecting = None
            self._pending_start = None
            self._pending_auto = False
            self._playing_speakers = None
            self._playing_auto = False
            try:
                await self.client.stop()
            except OwnToneError as e:
                log.warning("could not stop OwnTone: %s", e)
                self.last_error = str(e)
            await asyncio.to_thread(self.pipe.close)
            self._broadcast()

    async def play(self, speakers=None):
        if speakers is None:
            speakers = [o.id for o in self.outputs if o.selected] or list(self.settings.default_speakers)
        speakers = self._filter_available(speakers)
        if self.gate.play(self._clock()):
            prebuffer = list(self._prebuffer)
            self._collecting = []
            decided_at = self._clock()
            log.info("start decided: auto=False speakers=%s gate_state=%s prebuffer_chunks=%d",
                     speakers, self.gate.state.value, len(prebuffer))
            await self._start_playback(list(speakers), prebuffer=prebuffer, decided_at=decided_at)

    async def stop(self):
        if self.gate.stop(self._clock()):
            await self._stop_playback()

    def set_auto_on(self, enabled):
        self.gate.set_auto_on(enabled, self._clock())
        self.settings.auto_on = enabled
        self._save_settings()
        self._broadcast()

    def set_default_speakers(self, ids):
        self.settings.default_speakers = list(ids)
        self._save_settings()
        self._broadcast()

    async def set_input(self, device):
        self.settings.input = device
        self._save_settings()
        await self.capture.stop()
        self.capture.start(device)
        self._broadcast()

    async def list_inputs(self):
        inputs = await self._migrate_legacy_input()
        if inputs is None:
            inputs = await self._list_inputs()
        return [{"id": i["id"], "name": i["name"]} for i in inputs]

    def _is_available(self, output_id):
        presence = self._presence.get(output_id)
        return presence is not None and presence.state == "available"

    def _speaker_name(self, output_id):
        presence = self._presence.get(output_id)
        return presence.output.name if presence else output_id

    def _unavailable_error(self, output_id):
        return OwnToneError(f"{self._speaker_name(output_id)} is no longer available")

    async def set_speaker(self, output_id, *, selected=None, volume=None, pin=None):
        if self._first_contact_done and not self._is_available(output_id):
            raise self._unavailable_error(output_id)
        try:
            await self.client.update_output(output_id, selected=selected, volume=volume, pin=pin)
        except OwnToneError as e:
            await self.refresh_outputs()
            if not self._is_available(output_id):
                raise self._unavailable_error(output_id) from e
            output = next((o for o in self.outputs if o.id == output_id), None)
            if selected and output is not None and output.needs_pin:
                raise OwnToneError(f"{output.name} needs pairing: enter the PIN shown on the device") from e
            raise
        await self.refresh_outputs()
        if pin is not None and self.gate.state is State.PLAYING:
            ids = self._filter_available([o.id for o in self.outputs if o.selected])
            if output_id not in ids:
                ids.append(output_id)
            await self._start_playback(ids)
        self._broadcast()

    def _update_presence(self, raw_outputs):
        now = self._clock()
        first_contact = not self._first_contact_done
        self._first_contact_done = True
        raw_by_id = {o.id: o for o in raw_outputs}
        for oid, o in raw_by_id.items():
            presence = self._presence.get(oid)
            if presence is None:
                state = "available" if first_contact else "pending"
                self._presence[oid] = _Presence(o, state, now)
            elif presence.state == "gone":
                self._presence[oid] = _Presence(o, "available", now)
            else:
                presence.output = o
        for oid in list(self._presence):
            if oid in raw_by_id:
                continue
            presence = self._presence[oid]
            if presence.state == "pending":
                del self._presence[oid]
            elif presence.state == "available":
                presence.output = dataclasses.replace(presence.output, selected=False, needs_pin=False)
                presence.state = "gone"
                presence.since = now
        self._evaluate_presence(now)

    def _evaluate_presence(self, now):
        for oid, presence in list(self._presence.items()):
            if presence.state == "pending" and now - presence.since >= SPEAKER_APPEAR_DELAY_S:
                presence.state = "available"
            elif presence.state == "gone" and now - presence.since >= SPEAKER_GONE_GRACE_S:
                del self._presence[oid]

    async def refresh_outputs(self):
        self.outputs = await self.client.outputs()
        self._update_presence(self.outputs)

    async def handle_player_change(self):
        if self.gate.state is not State.PLAYING or self._pending_start is not None:
            return
        if self._lock.locked():
            return
        if self._clock() - self._started_at < EXTERNAL_STOP_GRACE_S:
            return
        if await self.client.player_state() != "play":
            if (self.gate.state is not State.PLAYING or self._lock.locked()
                    or self._pending_start is not None
                    or self._clock() - self._started_at < EXTERNAL_STOP_GRACE_S):
                return
            log.info("playback stopped outside tonearm; holding until the record ends")
            await self.stop()

    async def _owntone_loop(self):
        delay = 1.0
        while True:
            connected_at = self._clock()
            got_message = False
            try:
                await self.refresh_outputs()
                self.owntone_up = True
                self._broadcast()
                if not self._checked_leftover_playback:
                    self._checked_leftover_playback = True
                    if (self.gate.state is not State.PLAYING
                            and await self.client.player_state() == "play"
                            and await self.client.now_playing_path() == self.config.pipe_path):
                        async with self._lock:
                            if self.gate.state is not State.PLAYING:
                                await self.client.stop()
                async for events in self.client.notifications():
                    got_message = True
                    if "outputs" in events or "volume" in events:
                        await self.refresh_outputs()
                    if "player" in events:
                        await self.handle_player_change()
                    self._broadcast()
            except OwnToneError as e:
                log.warning("OwnTone unavailable: %s", e)
            except Exception:
                log.exception("unexpected error talking to OwnTone")
            if self.gate.state is State.PLAYING and self._pending_start is None and self._playing_speakers:
                healthy = False
                try:
                    state = await self.client.player_state()
                    path = await self.client.now_playing_path()
                    healthy = state == "play" and path == self.config.pipe_path
                except Exception:
                    pass
                if not healthy:
                    self._pending_start = list(self._playing_speakers)
                    self._pending_auto = self._playing_auto
                    self._pipe_uri = None
            self.owntone_up = False
            self._broadcast()
            if got_message or self._clock() - connected_at >= 10.0:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    def _save_settings(self):
        try:
            save(self.settings, self.config.settings_path)
        except OSError as e:
            log.error("could not save settings: %s", e)
            self.last_error = f"Could not save settings: {e}"

    def snapshot(self):
        countdown = self.gate.countdown(self._clock())
        defaults = list(self.settings.default_speakers)
        speaker_rows = []
        seen = set()
        for o in self.outputs:
            presence = self._presence.get(o.id)
            if presence is not None and presence.state == "available":
                speaker_rows.append(o)
                seen.add(o.id)
        for oid, presence in self._presence.items():
            if presence.state == "gone" and oid not in seen:
                speaker_rows.append(presence.output)
                seen.add(oid)
        shown_ids = {oid for oid, p in self._presence.items() if p.state != "pending"}
        return {
            "state": self.gate.state.value,
            "auto_on": self.gate.auto_on,
            "countdown_s": None if countdown is None else math.ceil(countdown - 1e-6),
            "level_db": round(self.level_db, 1),
            "stylus_down": self.gate.stylus_down,
            "input": self.settings.input,
            "input_available": self.input_available,
            "default_speakers": defaults,
            "missing_default_speakers": [i for i in defaults if i not in shown_ids] if self.outputs else [],
            "speakers": [
                {"id": o.id, "name": o.name, "type": o.type, "selected": o.selected,
                 "volume": o.volume, "needs_pin": o.needs_pin, "is_default": o.id in defaults,
                 "available": self._presence[o.id].state == "available"}
                for o in speaker_rows
            ],
            "owntone_up": self.owntone_up,
            "error": self.last_error,
        }

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    def _broadcast(self):
        self._last_level_broadcast = self._clock()
        snapshot = self.snapshot()
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(snapshot)
