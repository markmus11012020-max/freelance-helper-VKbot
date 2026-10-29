from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import load_settings
from app.db import Database
from app.health import build_health_app
from app.models import ParsedOrder


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "health.db"))
    await database.init()
    yield database
    await database.close()


def make_order(url: str, external_id: str = "1") -> ParsedOrder:
    return ParsedOrder(
        source="fl.ru",
        external_id=external_id,
        url=url,
        title="Test order",
        budget_text="30 000 ₽",
        budget_min=30000,
        budget_unit="project",
        published_at=datetime.now(timezone.utc),
    )


async def test_redirect_to_allowed_source(db: Database) -> None:
    order_id, _ = await db.upsert_order(make_order("https://www.fl.ru/projects/1/x.html"))
    app = build_health_app(replace(load_settings(), public_base_url="https://example.org"), db)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/go/{order_id}", allow_redirects=False)
        assert response.status == 302
        assert response.headers["Location"] == "https://www.fl.ru/projects/1/x.html"


async def test_redirect_unknown_order(db: Database) -> None:
    app = build_health_app(load_settings(), db)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/go/999", allow_redirects=False)
        assert response.status == 404


async def test_redirect_rejects_foreign_host(db: Database) -> None:
    order_id, _ = await db.upsert_order(make_order("https://evil.example.com/phish"))
    app = build_health_app(load_settings(), db)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/go/{order_id}", allow_redirects=False)
        assert response.status == 403
