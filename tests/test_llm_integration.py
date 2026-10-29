from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from aiohttp import web

from app.config import load_settings
from app.db import Database
from app.llm import LlmClient
from app.models import ParsedOrder, ScoreResult


@pytest.fixture
async def fake_openai():
    calls: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        calls.append(await request.json())
        content = json.dumps(
            {
                "score": 80,
                "fit": True,
                "reason": "Бот на Python, бюджет выше порога.",
                "red_flags": [],
            },
            ensure_ascii=False,
        )
        return web.json_response(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}/v1", calls
    finally:
        await runner.cleanup()


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "llm.db"))
    await database.init()
    yield database
    await database.close()


async def test_score_order_via_openai_compatible_endpoint(fake_openai, db: Database) -> None:
    base_url, calls = fake_openai
    settings = replace(
        load_settings(),
        openai_base_url=base_url,
        openai_api_key="test-key",
        openai_model="test-model",
        llm_enabled=True,
    )
    client = LlmClient(settings, db)
    try:
        order = ParsedOrder(
            source="fl.ru",
            external_id="1",
            url="https://example.com/1",
            title="Нужен бот на Python",
            description="Бот для приёма заявок",
            budget_text="40 000 ₽",
            budget_min=40000,
            budget_unit="project",
        )
        rule = ScoreResult(score=60, matched=["бот", "python"], reason="rules")

        data = await client.score_order(order, rule)
        assert data is not None
        assert data["score"] == 80
        assert data["fit"] is True
        assert "Python" in data["reason"]
        assert len(calls) == 1

        # Same prompt => cache, no second provider call and no extra tokens.
        again = await client.score_order(order, rule)
        assert again == data
        assert len(calls) == 1

        today = datetime.now(timezone.utc).date().isoformat()
        usage = await db.llm_usage_get(today)
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 5
        assert usage["calls"] == 1
    finally:
        await client.close()
