from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from dataclasses import replace

from app.backup import backup_loop
from app.config import Settings, load_settings
from app.db import Database
from app.filters import OrderFilter
from app.health import start_health_server
from app.llm import LlmClient
from app.monitor import Monitor
from app.notifier import Notifier
from app.parsers import build_parsers
from app.parsers.base import HttpClient
from app.vk.client import VkClient
from app.vk.handlers import BotHandlers
from app.vk.longpoll import VkLongPoll

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freelance monitor bot (VK)")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--dry-run", action="store_true", help="never send to VK, log cards instead")
    parser.add_argument("--send", action="store_true", help="with --once: actually send to VK")
    parser.add_argument("--init-db", action="store_true", help="create database and exit")
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    settings: Settings = load_settings()
    if args.dry_run or (args.once and not args.send):
        settings = replace(settings, dry_run=True)
    setup_logging(settings.log_level)
    os.environ.setdefault("TZ", settings.tz)

    db = Database(settings.database_path)
    await db.init()
    if settings.vk_chat_peer_id:
        await db.upsert_peer(
            settings.vk_chat_peer_id,
            kind="chat",
            role="chat",
            status="active",
            title="from env",
        )
    log.info(
        "started: db=%s dry_run=%s vk=%s llm=%s model=%s",
        settings.database_path,
        settings.dry_run,
        settings.vk_configured,
        settings.llm_configured,
        settings.openai_model,
    )

    if args.init_db:
        await db.close()
        return

    http = HttpClient(settings)
    await http.start()
    parsers = build_parsers(settings, http)
    order_filter = OrderFilter(settings)
    llm = LlmClient(settings, db)
    vk = VkClient(settings)
    notifier = Notifier(settings, db, vk)
    monitor = Monitor(settings, db, parsers, order_filter, llm, notifier)

    health_runner = None
    tasks: list[asyncio.Task[None]] = []
    try:
        if args.once:
            await monitor.scan_once()
            return

        health_runner = await start_health_server(settings, db)
        tasks.append(asyncio.create_task(monitor.run_forever(), name="monitor"))
        tasks.append(
            asyncio.create_task(
                backup_loop(settings.database_path, os.path.join(os.path.dirname(settings.database_path) or ".", "backups")),
                name="backup",
            )
        )

        if settings.vk_configured and not settings.dry_run:
            handlers = BotHandlers(
                settings, db, vk, notifier, llm, scan_now=monitor.scan_once
            )
            longpoll = VkLongPoll(vk, db, handlers.handle_update)
            tasks.append(asyncio.create_task(longpoll.run_forever(), name="vk-longpoll"))
        else:
            log.warning("VK is not configured or dry-run is on: running monitor only")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        if not stop.is_set():
            stop_task = asyncio.create_task(stop.wait(), name="stop")
            await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            stop_task.cancel()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if health_runner is not None:
            await health_runner.cleanup()
        await http.close()
        await vk.close()
        await llm.close()
        await db.close()
        log.info("stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
