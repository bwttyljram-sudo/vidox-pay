# -*- coding: utf-8 -*-
"""Tiny keep-alive web server (for UptimeRobot / Render). GET / and GET /health reply 'ok'."""
import logging

from aiohttp import web

log = logging.getLogger("vidoxpay.web")


async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="Vidox Pay is alive ✅")


async def start(port: int) -> None:
    app = web.Application()
    app.router.add_get("/", _ok)
    app.router.add_get("/health", _ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("keep-alive server on port %s", port)
