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
from app.drafts import DraftService
from app.embeddings import EmbeddingClient
from app.filters import OrderFilter
from app.github_sync import GitHubSync
from app.health import start_health_server
from app.llm import LlmClient
from app.memory import MemoryStore
from app.monitor import Monitor
from app.notifier import Notifier
from app.parsers import build_parsers
from app.parsers.base import HttpClient
from app.repo_memory import RepoMemory
from app.skills import SkillCatalog
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

    if os.getenv("PROBE_MODE", "").strip().lower() == "telegram":
        from app.telegram_probe import run_probe_server

        log.info("PROBE_MODE=telegram: starting Telegram connectivity probe")
        await run_probe_server(settings)
        return

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
        "started: db=%s dry_run=%s vk=%s llm=%s model=%s embeddings=%s github=%s",
        settings.database_path,
        settings.dry_run,
        settings.vk_configured,
        settings.llm_configured,
        settings.openai_model,
        settings.embeddings_configured,
        settings.github_configured,
    )

    if args.init_db:
        await db.close()
        return

    embeddings = EmbeddingClient(settings, db)
    repo_memory = RepoMemory(settings, db, embeddings)
    await repo_memory.load()
    memory = MemoryStore(settings, db)
    await memory.cleanup()
    skill_catalog = SkillCatalog(settings, db)
    await skill_catalog.ensure_backfilled()
    await skill_catalog.refresh()
    order_filter = OrderFilter(
        settings, skill_catalog.keywords, skill_catalog.disabled_stopwords
    )
    llm = LlmClient(settings, db)
    drafts = DraftService(settings, db, llm, repo_memory, memory)

    async def refresh_skills() -> None:
        await skill_catalog.refresh()
        order_filter.refresh(
            skill_catalog.keywords, skill_catalog.disabled_stopwords
        )

    github_sync = (
        GitHubSync(
            settings, db, llm, embeddings, repo_memory, on_sync=refresh_skills
        )
        if settings.github_configured
        else None
    )

    http = HttpClient(settings)
    await http.start()
    parsers = build_parsers(settings, http)
    vk = VkClient(settings)
    notifier = Notifier(settings, db, vk)
    monitor = Monitor(settings, db, parsers, order_filter, llm, notifier)

    health_runner = None
    tasks: list[asyncio.Task[None]] = []
    try:
        if args.once:
            await monitor.scan_once()
            return

        health_runner = await start_health_server(settings, db, repo_memory)
        tasks.append(asyncio.create_task(monitor.run_forever(), name="monitor"))
        tasks.append(
            asyncio.create_task(
                backup_loop(settings.database_path, os.path.join(os.path.dirname(settings.database_path) or ".", "backups")),
                name="backup",
            )
        )
        if github_sync is not None and not settings.dry_run:
            if not settings.github_token:
                log.warning(
                    "GITHUB_TOKEN is empty: GitHub sync is limited to 60 requests/hour"
                )
            tasks.append(
                asyncio.create_task(github_sync.run_forever(), name="github-sync")
            )

        if settings.vk_configured and not settings.dry_run:
            handlers = BotHandlers(
                settings,
                db,
                vk,
                notifier,
                llm,
                scan_now=monitor.scan_once,
                drafts=drafts,
                memory=memory,
                repos=repo_memory,
                github_sync=github_sync,
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
        if github_sync is not None:
            await github_sync.close()
        await http.close()
        await vk.close()
        await llm.close()
        await embeddings.close()
        await db.close()
        log.info("stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
