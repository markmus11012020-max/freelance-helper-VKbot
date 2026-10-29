from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from aiohttp import web

from app.config import Settings
from app.db import Database
from app.repo_memory import RepoMemory

log = logging.getLogger(__name__)

ALLOWED_REDIRECT_HOSTS = {
    "fl.ru",
    "www.fl.ru",
    "freelance.ru",
    "www.freelance.ru",
    "kwork.ru",
    "www.kwork.ru",
}


def build_health_app(
    settings: Settings, db: Database, repo_memory: RepoMemory | None = None
) -> web.Application:
    app = web.Application()

    async def healthz(_: web.Request) -> web.Response:
        stats = await db.stats()
        paused = await db.kv_get("paused", "0") == "1"
        last_scan = await db.kv_get("last_scan", "")
        repo_stats = await repo_memory.stats() if repo_memory is not None else {}
        if repo_stats:
            repo_stats["last_sync"] = await db.kv_get("github_last_sync", "")
            repo_stats["last_error"] = await db.kv_get("github_last_error", "")
        return web.json_response(
            {
                "status": "ok",
                "paused": paused,
                "last_scan": last_scan,
                "dry_run": settings.dry_run,
                "llm": settings.llm_configured,
                "embeddings": settings.embeddings_configured,
                "orders": stats,
                "repositories": repo_stats,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def root(_: web.Request) -> web.Response:
        return web.json_response({"service": "freelance-monitor", "ok": True})

    async def go_to_order(request: web.Request) -> web.Response:
        try:
            order_id = int(request.match_info["order_id"])
        except (KeyError, ValueError):
            raise web.HTTPNotFound(text="order not found")
        order = await db.get_order(order_id)
        if order is None:
            raise web.HTTPNotFound(text="order not found")
        url = str(order.get("url") or "")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or host not in ALLOWED_REDIRECT_HOSTS:
            raise web.HTTPForbidden(text="redirect is not allowed")
        raise web.HTTPFound(url, headers={"Cache-Control": "no-store"})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/health", healthz)
    app.router.add_get("/", root)
    app.router.add_get("/go/{order_id}", go_to_order)
    return app


async def start_health_server(
    settings: Settings, db: Database, repo_memory: RepoMemory | None = None
) -> web.AppRunner:
    app = build_health_app(settings, db, repo_memory)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    log.info("health server on :%s", settings.health_port)
    return runner
