from __future__ import annotations

import json
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import load_settings
from app.telegram_probe import build_probe_app, check_telegram


@pytest.fixture
async def fake_telegram():
    calls: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        method = request.match_info["method"]
        calls.append(method)
        if token == "bad":
            return web.json_response(
                {"ok": False, "error_code": 401, "description": "Unauthorized"},
                status=401,
            )
        if method == "getMe":
            return web.json_response(
                {
                    "ok": True,
                    "result": {
                        "id": 123456,
                        "is_bot": True,
                        "first_name": "Probe",
                        "username": "probe_bot",
                        "can_join_groups": True,
                        "can_read_all_group_messages": False,
                    },
                }
            )
        if method == "getUpdates":
            return web.json_response(
                {
                    "ok": True,
                    "result": [
                        {"update_id": 101, "message": {"message_id": 1, "text": "/start"}}
                    ],
                }
            )
        return web.json_response({"ok": False, "description": "not found"}, status=404)

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", calls
    finally:
        await runner.cleanup()


def make_settings(base_url: str, token: str = "123:abc"):
    return replace(
        load_settings(),
        telegram_bot_token=token,
        telegram_api_url=base_url,
        telegram_proxy="",
    )


async def test_probe_success(fake_telegram) -> None:
    base_url, calls = fake_telegram
    report = await check_telegram(
        make_settings(base_url), attempts=3, poll_timeout=1, check_ip=False
    )
    assert report["ok"] is True
    assert report["success"] == 3
    assert report["attempts"][0]["bot"]["username"] == "probe_bot"
    assert report["poll"]["ok"] is True
    assert report["poll"]["updates"] == 1
    assert report["poll"]["last_update_id"] == 101
    assert "123:abc" not in json.dumps(report)
    assert calls.count("getMe") == 3


async def test_probe_bad_token(fake_telegram) -> None:
    base_url, _ = fake_telegram
    report = await check_telegram(
        make_settings(base_url, token="bad"), attempts=2, poll_timeout=1, check_ip=False
    )
    assert report["ok"] is False
    assert report["success"] == 0
    assert "Unauthorized" in report["error"]


async def test_probe_without_token() -> None:
    report = await check_telegram(
        replace(load_settings(), telegram_bot_token=""), attempts=1, check_ip=False
    )
    assert report["ok"] is False
    assert "TELEGRAM_BOT_TOKEN" in report["error"]


async def test_probe_http_endpoint(fake_telegram) -> None:
    base_url, _ = fake_telegram
    app = build_probe_app(make_settings(base_url))
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz?attempts=1&poll=0&ip=0")
        assert response.status == 200
        payload = await response.json()
        assert payload["ok"] is True
        assert payload["route"] == "direct"
        assert payload["attempts"][0]["ok"] is True
        assert "poll" not in payload
