"""Send recent orders above MIN_SCORE to active chats.

Usage (inside the container):
    python -m scripts.backfill_deliveries --limit 20
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import load_settings
from app.db import Database
from app.notifier import Notifier
from app.vk.client import VkClient


async def run() -> None:
    parser = argparse.ArgumentParser(description="Backfill recent orders to active VK chats")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    settings = load_settings()
    db = Database(settings.database_path)
    await db.init()
    vk = VkClient(settings)
    notifier = Notifier(settings, db, vk)
    try:
        targets = await db.delivery_targets()
        if not targets:
            print("no active VK targets; send /connect in the chat first")
            return
        orders = await db.orders_for_backfill(
            limit=max(1, args.limit), min_score=settings.min_score
        )
        for order in orders:
            for target in targets:
                await db.enqueue_delivery(int(order["id"]), int(target["peer_id"]))
        sent = await notifier.deliver_pending(limit=max(30, args.limit * len(targets)))
        print(
            f"orders={len(orders)} targets={len(targets)} "
            f"queued={len(orders) * len(targets)} sent={sent} min_score={settings.min_score}"
        )
    finally:
        await vk.close()
        await db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
