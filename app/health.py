from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiohttp import web

from app.config import Settings
from app.db import Database

log = logging.getLogger(__name__)


def build_health_app(settings: Settings, db: Database) -> web.Application:
    app = web.Application()

    async def healthz(_: web.Request) -> web.Response:
        stats = await db.stats()
        paused = await db.kv_get("paused", "0") == "1"
        last_scan = await db.kv_get("last_scan", "")
        return web.json_response(
            {
                "status": "ok",
                "paused": paused,
                "last_scan": last_scan,
                "dry_run": settings.dry_run,
                "llm": settings.llm_configured,
                "orders": stats,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def root(_: web.Request) -> web.Response:
        return web.json_response({"service": "freelance-monitor", "ok": True})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/health", healthz)
    app.router.add_get("/", root)
    return app


async def start_health_server(settings: Settings, db: Database) -> web.AppRunner:
    app = build_health_app(settings, db)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    log.info("health server on :%s", settings.health_port)
    return runner
