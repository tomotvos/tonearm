import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Settings:
    input: str | None = None
    default_speakers: list[str] = field(default_factory=list)
    auto_on: bool = True


def load(path: Path) -> Settings:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable settings file %s: %s", path, e)
        return Settings()
    if not isinstance(data, dict):
        log.warning("ignoring settings file %s: not a JSON object", path)
        return Settings()
    settings = Settings()
    if isinstance(data.get("input"), str):
        settings.input = data["input"]
    if isinstance(data.get("default_speakers"), list):
        settings.default_speakers = [s for s in data["default_speakers"] if isinstance(s, str)]
    if isinstance(data.get("auto_on"), bool):
        settings.auto_on = data["auto_on"]
    return settings


def save(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2))
    os.replace(tmp, path)
