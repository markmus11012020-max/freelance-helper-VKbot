from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.db import Database
from app.textutil import truncate

log = logging.getLogger(__name__)


class MemoryStore:
    """Short-term dialogue memory + explicit long-term user facts."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    @staticmethod
    def scope(user_id: int) -> str:
        return f"user:{user_id}"

    async def record(
        self,
        *,
        peer_id: int,
        user_id: int,
        role: str,
        content: str,
        order_id: int | None = None,
    ) -> None:
        content = " ".join((content or "").split())
        if not content or user_id <= 0:
            return
        await self.db.memory_add(
            self.scope(user_id),
            peer_id=peer_id,
            user_id=user_id,
            role=role,
            content=content,
            order_id=order_id,
            ttl_days=self.settings.short_memory_ttl_days,
            max_messages=self.settings.short_memory_max_messages,
        )

    async def context(self, user_id: int, limit: int | None = None) -> str:
        rows = await self.db.memory_recent(
            self.scope(user_id), limit=limit or self.settings.short_memory_max_messages
        )
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            role = "Пользователь" if row.get("role") == "user" else "Бот"
            lines.append(f"{role}: {truncate(str(row.get('content') or ''), 800)}")
        return "\n".join(lines)

    async def facts(self, user_id: int) -> list[dict[str, Any]]:
        return await self.db.user_memory_list(user_id)

    async def facts_text(self, user_id: int) -> str:
        facts = await self.facts(user_id)
        if not facts:
            return ""
        return "\n".join(f"- {truncate(str(item['fact']), 300)}" for item in facts)

    async def remember_fact(self, user_id: int, fact: str) -> None:
        await self.db.user_memory_add(
            user_id, fact, max_facts=self.settings.user_memory_max_facts
        )

    async def forget(self, user_id: int, fact_id: int | None = None) -> None:
        await self.db.user_memory_delete(user_id, fact_id)

    async def clear(self, user_id: int) -> None:
        await self.db.memory_clear(self.scope(user_id))

    async def cleanup(self) -> None:
        await self.db.memory_cleanup()

    async def count(self, user_id: int | None = None) -> int:
        return await self.db.memory_count(self.scope(user_id) if user_id else None)
