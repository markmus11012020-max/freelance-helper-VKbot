from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db import Database
from app.models import ParsedOrder


def make_order(external_id: str = "42") -> ParsedOrder:
    return ParsedOrder(
        source="fl.ru",
        external_id=external_id,
        url=f"https://www.fl.ru/projects/{external_id}/x.html",
        title="Парсер на Python",
        description="Собрать данные",
        budget_text="30 000 ₽",
        budget_min=30000,
        budget_max=30000,
        budget_unit="project",
        published_at=datetime.now(timezone.utc),
    )


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


async def test_order_dedup(db: Database) -> None:
    order = make_order()
    first_id, first_new = await db.upsert_order(order)
    second_id, second_new = await db.upsert_order(order)
    assert first_new is True
    assert second_new is False
    assert first_id == second_id
    stats = await db.stats()
    assert stats["total"] == 1


async def test_peers_and_deliveries(db: Database) -> None:
    order_id, _ = await db.upsert_order(make_order())
    chat_peer = 2_000_000_123
    await db.upsert_peer(chat_peer, kind="chat", role="chat", status="active", title="test")
    targets = await db.delivery_targets()
    assert [t["peer_id"] for t in targets] == [chat_peer]

    await db.enqueue_delivery(order_id, chat_peer)
    await db.enqueue_delivery(order_id, chat_peer)
    pending = await db.pending_deliveries()
    assert len(pending) == 1

    await db.mark_delivery(int(pending[0]["id"]), "sent")
    assert await db.pending_deliveries() == []

    stats = await db.stats()
    assert stats["sent"] == 1


async def test_kv_and_source_state(db: Database) -> None:
    assert await db.kv_get("missing", "fallback") == "fallback"
    await db.kv_set("paused", "1")
    assert await db.kv_get("paused") == "1"

    assert await db.source_fail("fl.ru", "boom") == 1
    assert await db.source_fail("fl.ru", "boom") == 2
    await db.source_ok("fl.ru")
    sources = await db.sources()
    assert sources[0]["fail_count"] == 0
    assert sources[0]["ok"] == 1
