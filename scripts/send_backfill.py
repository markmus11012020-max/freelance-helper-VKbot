"""Send the latest high-score orders to the connected VK chat(s).

Usage (inside the container):
    python -m scripts.send_backfill
"""

from __future__ import annotations

import asyncio

from app.config import load_settings
from app.db import Database
from app.notifier import Notifier
from app.vk.client import VkClient


async def main() -> int:
    settings = load_settings()
    db = Database(settings.database_path)
    await db.init()
    vk = VkClient(settings)
    notifier = Notifier(settings, db, vk)
    try:
        targets = await db.delivery_targets()
        if not targets:
            print("no active VK chats: send /connect first")
            return 1
        rows = await db.orders_for_backfill(limit=10, min_score=settings.min_score)
        for target in targets:
            for row in rows:
                await db.enqueue_delivery(int(row["id"]), int(target["peer_id"]))
        print(f"targets={len(targets)} candidates={len(rows)}")
        sent = await notifier.deliver_pending(limit=50)
        print(f"sent={sent}")
        return 0
    finally:
        await vk.close()
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
