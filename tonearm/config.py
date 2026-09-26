import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Config:
    owntone_url: str
    pipe_path: str
    quiet_timeout_s: float
    start_db: float
    floor_db: float
    settings_path: Path
    port: int
    web_dir: Path

    def __post_init__(self):
        if self.start_db <= self.floor_db:
            raise ValueError("TONEARM_START_DB must be greater than TONEARM_FLOOR_DB")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        return cls(
            owntone_url=env.get("OWNTONE_URL", "http://localhost:3689").rstrip("/"),
            pipe_path=env.get("TONEARM_PIPE", "/srv/music/Turntable"),
            quiet_timeout_s=float(env.get("TONEARM_QUIET_TIMEOUT_S", "180")),
            start_db=float(env.get("TONEARM_START_DB", "-65")),
            floor_db=float(env.get("TONEARM_FLOOR_DB", "-80")),
            settings_path=Path(env.get("TONEARM_SETTINGS", "~/.config/tonearm/settings.json")).expanduser(),
            port=int(env.get("TONEARM_PORT", "8080")),
            web_dir=Path(__file__).resolve().parent.parent / "web",
        )
