from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from app.db import Database
from app.vk.client import VkClient

log = logging.getLogger(__name__)

UpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]


class VkLongPoll:
    """VK Bots Long Poll loop with ts/key/server recovery."""

    def __init__(self, client: VkClient, db: Database, handler: UpdateHandler) -> None:
        self.client = client
        self.db = db
        self.handler = handler

    async def run_forever(self) -> None:
        server: str | None = None
        key: str | None = None
        ts: str | None = await self.db.kv_get("vk_ts") or None

        while True:
            try:
                if not server or not key:
                    data = await self.client.get_long_poll_server()
                    server = str(data["server"])
                    key = str(data["key"])
                    if not ts:
                        ts = str(data["ts"])
                    log.info("VK long poll connected: server=%s ts=%s", server, ts)

                payload = await self.client.long_poll(server, key, ts or "0")
                failed = payload.get("failed")

                if failed == 1:
                    ts = str(payload.get("ts") or ts)
                    log.debug("VK long poll: history gap, new ts=%s", ts)
                    continue
                if failed == 2:
                    log.warning("VK long poll: invalid key, refreshing")
                    data = await self.client.get_long_poll_server()
                    key = str(data["key"])
                    ts = str(data.get("ts") or ts)
                    continue
                if failed == 3:
                    log.warning("VK long poll: server info lost, reconnecting")
                    server = None
                    key = None
                    ts = None
                    continue

                ts = str(payload.get("ts") or ts)
                for update in payload.get("updates") or []:
                    try:
                        await self.handler(update)
                    except Exception:
                        log.exception("VK update handler failed: %s", update.get("type"))
                if ts:
                    await self.db.kv_set("vk_ts", ts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("VK long poll error: %s", exc)
                await asyncio.sleep(5)
                server = None
                key = None
