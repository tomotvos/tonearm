import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import aiohttp


class OwnToneError(Exception):
    pass


@dataclass(frozen=True)
class Output:
    id: str
    name: str
    type: str
    selected: bool
    volume: int
    needs_pin: bool

    @classmethod
    def from_json(cls, d: dict) -> "Output":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", "")),
            type=str(d.get("type", "")),
            selected=bool(d.get("selected", False)),
            volume=int(d.get("volume", 0)),
            needs_pin=bool(d.get("needs_auth_key", False)),
        )


class OwnToneClient:
    def __init__(self, base_url: str, session: aiohttp.ClientSession, timeout_s: float = 5.0,
                 play_timeout_s: float = 20.0):
        self._base = base_url.rstrip("/")
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._play_timeout = aiohttp.ClientTimeout(total=play_timeout_s)

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None, timeout: aiohttp.ClientTimeout | None = None) -> Any:
        try:
            async with self._session.request(method, self._base + path, params=params, json=body,
                                             timeout=timeout or self._timeout) as resp:
                if resp.status >= 400:
                    raise OwnToneError(f"{method} {path} returned HTTP {resp.status}")
                text = await resp.text()
                return json.loads(text) if text.strip() else None
        except json.JSONDecodeError as e:
            raise OwnToneError(f"{method} {path} returned invalid JSON") from e
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise OwnToneError(f"{method} {path} failed: {str(e) or type(e).__name__}") from e

    async def outputs(self) -> list[Output]:
        data = await self._request("GET", "/api/outputs")
        try:
            return [Output.from_json(o) for o in (data or {}).get("outputs", [])]
        except (KeyError, ValueError, TypeError) as e:
            raise OwnToneError(f"unexpected /api/outputs response: {e!r}") from e

    async def set_outputs(self, ids: list[str]) -> None:
        await self._request("PUT", "/api/outputs/set", body={"outputs": list(ids)})

    async def update_output(self, output_id: str, *, selected: bool | None = None,
                            volume: int | None = None, pin: str | None = None) -> None:
        body = {k: v for k, v in (("selected", selected), ("volume", volume), ("pin", pin)) if v is not None}
        await self._request("PUT", f"/api/outputs/{output_id}", body=body)

    async def find_track_uri(self, path: str) -> str | None:
        data = await self._request("GET", "/api/search",
                                   params={"type": "tracks", "expression": f'path is "{path}"'})
        items = (data or {}).get("tracks", {}).get("items", [])
        if not items:
            return None
        try:
            return items[0]["uri"]
        except (KeyError, TypeError, AttributeError) as e:
            raise OwnToneError(f"unexpected /api/search response: {e!r}") from e

    async def play_uri(self, uri: str) -> None:
        await self._request("POST", "/api/queue/items/add",
                            params={"uris": uri, "clear": "true", "playback": "start"},
                            timeout=self._play_timeout)

    async def stop(self) -> None:
        await self._request("PUT", "/api/player/stop")

    async def player_state(self) -> str:
        data = await self._request("GET", "/api/player")
        return str((data or {}).get("state", "stop"))

    async def now_playing_path(self) -> str | None:
        player = await self._request("GET", "/api/player")
        item_id = (player or {}).get("item_id")
        if item_id is None:
            return None
        data = await self._request("GET", "/api/queue")
        try:
            for item in (data or {}).get("items", []):
                if item["id"] == item_id:
                    return item.get("path")
            return None
        except (KeyError, TypeError, AttributeError) as e:
            raise OwnToneError(f"unexpected /api/queue response: {e!r}") from e

    async def notifications(self, events=("outputs", "player", "volume")) -> AsyncIterator[list[str]]:
        config = await self._request("GET", "/api/config")
        port = (config or {}).get("websocket_port")
        if port is None:
            raise OwnToneError("OwnTone /api/config has no websocket_port")
        host = urlparse(self._base).hostname
        url = f"ws://{host}:{port}"
        try:
            async with self._session.ws_connect(url, protocols=("notify",)) as ws:
                await ws.send_json({"notify": list(events)})
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        yield json.loads(msg.data).get("notify", [])
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise OwnToneError(f"websocket {url} failed: {str(e) or type(e).__name__}") from e
