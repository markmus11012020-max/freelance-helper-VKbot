from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.config import load_settings
from app.db import Database
from app.memory import MemoryStore


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "memory.db"))
    await database.init()
    yield database
    await database.close()


async def test_short_memory_keeps_recent_and_clears(db: Database) -> None:
    settings = replace(load_settings(), short_memory_max_messages=3)
    memory = MemoryStore(settings, db)
    for index in range(5):
        await memory.record(
            peer_id=1,
            user_id=1,
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}",
        )
    recent = await memory.context(1)
    assert "message 2" in recent
    assert "message 4" in recent
    assert "message 0" not in recent
    assert await memory.count(1) == 3

    await memory.clear(1)
    assert await memory.count(1) == 0


async def test_short_memory_ttl_cleanup(db: Database) -> None:
    memory = MemoryStore(load_settings(), db)
    await memory.record(peer_id=1, user_id=1, role="user", content="temporary")
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    await db.memory_cleanup(now=future)
    assert await memory.count(1) == 0


async def test_long_term_facts_are_deduplicated_and_limited(db: Database) -> None:
    settings = replace(load_settings(), user_memory_max_facts=3)
    memory = MemoryStore(settings, db)
    for index in range(5):
        await memory.remember_fact(1, f"fact {index}")
    await memory.remember_fact(1, "fact 4")
    facts = await memory.facts(1)
    assert len(facts) == 3
    assert [item["fact"] for item in facts] == ["fact 4", "fact 3", "fact 2"]

    await memory.forget(1, int(facts[0]["id"]))
    assert len(await memory.facts(1)) == 2
    await memory.forget(1, None)
    assert await memory.facts(1) == []
