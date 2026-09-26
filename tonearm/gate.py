from dataclasses import dataclass
from enum import Enum


class State(str, Enum):
    ARMED = "armed"
    PLAYING = "playing"
    HELD = "held"
    OFF = "off"


class Action(str, Enum):
    START_AUTO = "start_auto"
    START_MANUAL = "start_manual"
    STOP = "stop"


@dataclass(frozen=True)
class GateConfig:
    start_db: float
    floor_db: float
    quiet_timeout_s: float = 180.0
    start_hold_s: float = 1.0

    def __post_init__(self):
        if self.start_db <= self.floor_db:
            raise ValueError("start_db must be greater than floor_db")


class Gate:
    def __init__(self, config: GateConfig, auto_on: bool = True, now: float = 0.0):
        self._cfg = config
        self._auto_on = auto_on
        self._state = State.ARMED if auto_on else State.OFF
        self._state_since = now
        self._down = False
        self._reading_since = now

    @property
    def state(self) -> State:
        return self._state

    @property
    def auto_on(self) -> bool:
        return self._auto_on

    @property
    def stylus_down(self) -> bool:
        return self._down

    def feed(self, level_db: float, now: float) -> list[Action]:
        if level_db >= self._cfg.start_db:
            down = True
        elif level_db <= self._cfg.floor_db:
            down = False
        else:
            down = self._down
        if down != self._down:
            self._down = down
            self._reading_since = now
        return self.tick(now)

    def tick(self, now: float) -> list[Action]:
        elapsed = self._elapsed(now)
        if self._state is State.ARMED and self._down and elapsed >= self._cfg.start_hold_s:
            self._enter(State.PLAYING, now)
            return [Action.START_AUTO]
        if self._state in (State.PLAYING, State.HELD) and not self._down and elapsed >= self._cfg.quiet_timeout_s:
            was_playing = self._state is State.PLAYING
            self._enter(State.ARMED if self._auto_on else State.OFF, now)
            return [Action.STOP] if was_playing else []
        return []

    def play(self, now: float) -> list[Action]:
        if self._state is State.PLAYING:
            return []
        self._enter(State.PLAYING, now)
        return [Action.START_MANUAL]

    def stop(self, now: float) -> list[Action]:
        if self._state is not State.PLAYING:
            return []
        self._enter(State.HELD, now)
        return [Action.STOP]

    def set_auto_on(self, enabled: bool, now: float) -> None:
        self._auto_on = enabled
        if not enabled and self._state is State.ARMED:
            self._enter(State.OFF, now)
        elif enabled and self._state is State.OFF:
            self._enter(State.ARMED, now)

    def countdown(self, now: float) -> float | None:
        if self._state in (State.PLAYING, State.HELD) and not self._down:
            return max(0.0, self._cfg.quiet_timeout_s - self._elapsed(now))
        return None

    def _elapsed(self, now: float) -> float:
        return now - max(self._reading_since, self._state_since)

    def _enter(self, state: State, now: float) -> None:
        self._state = state
        self._state_since = now
