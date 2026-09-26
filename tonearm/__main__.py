import asyncio
import inspect
import logging
import signal

import aiohttp
from aiohttp import web

from .api import create_app
from .capture import Capture
from .config import Config
from .owntone import OwnToneClient
from .pipe import PipeWriter
from .service import Service
from .settings import load

SHUTDOWN_TIMEOUT_S = 1.0


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    async with aiohttp.ClientSession() as session:
        service = Service(config, OwnToneClient(config.owntone_url, session),
                          PipeWriter(config.pipe_path, max_chunks=600),
                          load(config.settings_path), capture_factory=Capture)
        app = create_app(service, config.web_dir)
        if "shutdown_timeout" in inspect.signature(web.BaseRunner).parameters:
            runner = web.AppRunner(app, shutdown_timeout=SHUTDOWN_TIMEOUT_S)
            site_kwargs = {}
        else:
            runner = web.AppRunner(app)
            site_kwargs = {"shutdown_timeout": SHUTDOWN_TIMEOUT_S}
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.port, **site_kwargs).start()
        await service.start()
        logging.getLogger(__name__).info("listening on port %d", config.port)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
        try:
            await stop.wait()
        finally:
            await service.close()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
