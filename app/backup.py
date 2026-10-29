from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def backup_once(db_path: str, backup_dir: str, keep: int = 8) -> Path:
    source_path = Path(db_path)
    if not source_path.exists():
        raise FileNotFoundError(db_path)
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target_path = target_dir / f"bot-{stamp}.db"

    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    backups = sorted(target_dir.glob("bot-*.db"))
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            log.warning("cannot remove old backup %s", old)
    log.info("database backup: %s", target_path)
    return target_path


async def backup_loop(db_path: str, backup_dir: str, interval_seconds: int = 21_600) -> None:
    if db_path == ":memory:":
        return
    while True:
        try:
            await asyncio.to_thread(backup_once, db_path, backup_dir)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("database backup failed")
        await asyncio.sleep(interval_seconds)
