from __future__ import annotations

from dataclasses import replace

from app.config import load_settings
from app.llm import LlmClient, _strip_json
from app.notifier import Notifier


class DummyDb:
    async def llm_cache_get(self, key: str) -> str | None:  # pragma: no cover
        return None

    async def llm_usage_get(self, day: str) -> dict:  # pragma: no cover
        return {}

    async def llm_usage_add(self, day: str, prompt: int, completion: int) -> None:  # pragma: no cover
        return None

    async def llm_cache_put(self, key: str, response: str) -> None:  # pragma: no cover
        return None


def test_strip_json_handles_markdown_fence() -> None:
    raw = '```json\n{"score": 80, "fit": true, "reason": "ok"}\n```'
    assert _strip_json(raw) == '{"score": 80, "fit": true, "reason": "ok"}'


def test_llm_disabled_without_key() -> None:
    settings = replace(load_settings(), openai_api_key="", llm_enabled=True)
    assert not LlmClient(settings, DummyDb()).available  # type: ignore[arg-type]


def test_order_keyboard_uses_order_id_for_delivery_rows() -> None:
    row = {
        "id": 15,  # delivery id
        "order_id": 7,
        "url": "https://example.com/order",
    }
    keyboard = Notifier.order_keyboard(row)
    assert keyboard is not None
    payload = keyboard["buttons"][0][0]["action"]["payload"]
    assert '"order_id": 7' in payload
