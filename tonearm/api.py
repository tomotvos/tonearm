import asyncio
import json
from pathlib import Path

from aiohttp import web

from .owntone import OwnToneError

SSE_KEEPALIVE_S = 15.0


def _bad(message):
    return web.HTTPBadRequest(text=json.dumps({"error": message}), content_type="application/json")


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


async def _body(request):
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except ValueError:
        raise _bad("body must be valid JSON")
    if not isinstance(data, dict):
        raise _bad("body must be a JSON object")
    return data


def _sse(snapshot):
    return f"data: {json.dumps(snapshot)}\n\n".encode()


def create_app(service, web_dir: Path) -> web.Application:
    def ok():
        return web.json_response(service.snapshot())

    async def status(request):
        return ok()

    async def play(request):
        speakers = (await _body(request)).get("speakers")
        if speakers is not None and not _is_str_list(speakers):
            raise _bad("speakers must be a list of speaker ids")
        await asyncio.shield(service.play(speakers))
        return ok()

    async def stop(request):
        await asyncio.shield(service.stop())
        return ok()

    async def auto_on(request):
        enabled = (await _body(request)).get("enabled")
        if not isinstance(enabled, bool):
            raise _bad("enabled must be true or false")
        service.set_auto_on(enabled)
        return ok()

    async def speaker(request):
        if not request.match_info["id"].isdigit():
            raise _bad("unknown speaker id")
        data = await _body(request)
        fields = {}
        if "selected" in data:
            if not isinstance(data["selected"], bool):
                raise _bad("selected must be true or false")
            fields["selected"] = data["selected"]
        if "volume" in data:
            volume = data["volume"]
            if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
                raise _bad("volume must be an integer from 0 to 100")
            fields["volume"] = volume
        if "pin" in data:
            pin = data["pin"]
            if not (isinstance(pin, str) and len(pin) == 4 and pin.isdigit()):
                raise _bad("pin must be 4 digits")
            fields["pin"] = pin
        if not fields:
            raise _bad("send at least one of selected, volume or pin")
        try:
            await asyncio.shield(service.set_speaker(request.match_info["id"], **fields))
        except OwnToneError as e:
            return web.json_response({"error": str(e)}, status=502)
        return ok()

    async def default_speakers(request):
        ids = (await _body(request)).get("ids")
        if not _is_str_list(ids):
            raise _bad("ids must be a list of speaker ids")
        service.set_default_speakers(ids)
        return ok()

    async def inputs(request):
        return web.json_response({"inputs": await service.list_inputs()})

    async def set_input(request):
        device = (await _body(request)).get("id")
        if not isinstance(device, str):
            raise _bad("id must be a string")
        if device not in {i["id"] for i in await service.list_inputs()}:
            raise _bad("unknown input")
        await service.set_input(device)
        return ok()

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        queue = service.subscribe()
        try:
            await resp.write(_sse(service.snapshot()))
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                await resp.write(_sse(snapshot))
        except ConnectionResetError:
            pass
        finally:
            service.unsubscribe(queue)
        return resp

    async def index(request):
        return web.FileResponse(web_dir / "index.html")

    async def favicon(request):
        return web.FileResponse(web_dir / "favicon.ico", headers={"Content-Type": "image/x-icon"})

    async def touch_icon(request):
        return web.FileResponse(web_dir / "apple-touch-icon.png")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/apple-touch-icon.png", touch_icon)
    app.router.add_get("/apple-touch-icon-precomposed.png", touch_icon)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/play", play)
    app.router.add_post("/api/stop", stop)
    app.router.add_put("/api/auto-on", auto_on)
    app.router.add_put("/api/speakers/{id}", speaker)
    app.router.add_put("/api/default-speakers", default_speakers)
    app.router.add_get("/api/inputs", inputs)
    app.router.add_put("/api/input", set_input)
    app.router.add_static("/static/", web_dir)
    return app
