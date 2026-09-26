import asyncio

from aiohttp import web

OUTPUTS = {
    "outputs": [
        {"id": "139080632609167", "name": "Bedroom", "type": "AirPlay 2", "selected": False,
         "has_password": False, "requires_auth": False, "needs_auth_key": False, "volume": 50},
        {"id": "266176521832340", "name": "Living Room", "type": "AirPlay 2", "selected": False,
         "has_password": False, "requires_auth": True, "needs_auth_key": True, "volume": 40},
        {"id": "0", "name": "Computer", "type": "ALSA", "selected": True,
         "has_password": False, "requires_auth": False, "needs_auth_key": False, "volume": 50},
    ]
}


class FakeOwnTone:
    def __init__(self):
        self.requests = []
        self.fail_status = None
        self.tracks = [{"id": 1, "uri": "library:track:1", "path": "/srv/music/Turntable"}]
        self.player = "stop"
        self.item_id = None
        self.queue_items = []
        self.ws_port = None
        self.subscribed = None
        self.notify_queue = asyncio.Queue()
        self.raw_outputs_body = None
        self.config_body = None
        self.delay_s = 0

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._record])
        app.router.add_get("/", self._ws)
        app.router.add_get("/api/config", self._config)
        app.router.add_get("/api/outputs", self._outputs)
        app.router.add_put("/api/outputs/set", self._no_content)
        app.router.add_put("/api/outputs/{id}", self._no_content)
        app.router.add_get("/api/search", self._search)
        app.router.add_post("/api/queue/items/add", self._queue_add)
        app.router.add_put("/api/player/stop", self._no_content)
        app.router.add_get("/api/player", self._player)
        app.router.add_get("/api/queue", self._queue)
        return app

    async def _config(self, request):
        if self.config_body is not None:
            return web.Response(text=self.config_body, content_type="application/json")
        return web.json_response({"websocket_port": self.ws_port})

    async def _outputs(self, request):
        if self.raw_outputs_body is not None:
            return web.Response(text=self.raw_outputs_body, content_type="application/json")
        return web.json_response(OUTPUTS)

    async def _search(self, request):
        return web.json_response({"tracks": {"items": self.tracks, "total": len(self.tracks)}})

    async def _queue_add(self, request):
        return web.json_response({"count": 1})

    async def _player(self, request):
        body = {"state": self.player}
        if self.item_id is not None:
            body["item_id"] = self.item_id
        return web.json_response(body)

    async def _queue(self, request):
        return web.json_response({"items": self.queue_items})

    @web.middleware
    async def _record(self, request, handler):
        if request.path != "/":
            body = await request.json() if request.can_read_body else None
            self.requests.append((request.method, request.path, dict(request.query), body))
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.fail_status:
                return web.Response(status=self.fail_status)
        return await handler(request)

    async def _no_content(self, request):
        return web.Response(status=204)

    async def _ws(self, request):
        ws = web.WebSocketResponse(protocols=("notify",))
        await ws.prepare(request)
        self.subscribed = await ws.receive_json()
        while (events := await self.notify_queue.get()) is not None:
            await ws.send_json({"notify": events})
        await ws.close()
        return ws
