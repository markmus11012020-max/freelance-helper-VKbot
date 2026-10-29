from __future__ import annotations

from dataclasses import replace

from app.config import load_settings
from app.notifier import Notifier


class DummyDb:
    pass


def test_card_contains_key_fields() -> None:
    notifier = Notifier(load_settings(), DummyDb(), None)  # type: ignore[arg-type]
    row = {
        "id": 7,
        "score": 82,
        "source": "fl.ru",
        "title": "Нужен VK-бот на Python",
        "url": "https://www.fl.ru/projects/1/x.html",
        "description": "Бот для приёма заявок и уведомлений.",
        "budget_text": "40 000 ₽",
        "budget_min": 40000,
        "budget_unit": "project",
        "category": "Сайты / Разработка",
        "published_at": "2026-10-29T07:30:00+00:00",
        "llm_reason": "Подходит: бот + Python, бюджет выше порога.",
        "matched_keywords": '["vk бот", "python"]',
    }
    text = notifier.build_card(row)
    assert "82/100" in text
    assert "Нужен VK-бот на Python" in text
    assert "40 000 ₽" in text
    assert "https://www.fl.ru/projects/1/x.html" in text
    assert len(text) <= 4000

    keyboard = notifier.order_keyboard(row)
    assert keyboard is not None
    assert keyboard["inline"] is True
    assert len(keyboard["buttons"][0]) == 2


def test_dry_run_delivery_is_noop() -> None:
    import asyncio

    settings = replace(load_settings(), dry_run=True)
    notifier = Notifier(settings, DummyDb(), None)  # type: ignore[arg-type]
    assert asyncio.run(notifier.deliver_pending()) == 0


def test_order_keyboard_can_omit_link() -> None:
    row = {"order_id": 7, "url": "https://kwork.ru/projects/1/view"}
    keyboard = Notifier.order_keyboard(row, include_link=False)
    assert keyboard is not None
    assert len(keyboard["buttons"][0]) == 1


def test_send_order_retries_without_link_on_vk_911() -> None:
    import asyncio

    from app.vk.client import VkApiError

    class FallbackVk:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_message(self, peer_id, message, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise VkApiError(
                    "messages.send",
                    911,
                    "Keyboard format is invalid: button [0][1] has invalid link",
                )
            return 1

    settings = replace(load_settings(), dry_run=False, vk_group_token="test", vk_group_id=1)
    vk = FallbackVk()
    notifier = Notifier(settings, DummyDb(), vk)  # type: ignore[arg-type]
    row = {
        "id": 7,
        "score": 61,
        "source": "kwork.ru",
        "title": "Скопировать тг-бота",
        "url": "https://kwork.ru/projects/3264756/view",
        "budget_text": "36 000 ₽",
    }
    asyncio.run(notifier.send_order(row, 2_000_000_001))
    assert len(vk.calls) == 2
    first_buttons = vk.calls[0]["keyboard"]["buttons"][0]
    second_buttons = vk.calls[1]["keyboard"]["buttons"][0]
    assert len(first_buttons) == 2
    assert len(second_buttons) == 1
    assert vk.calls[0]["random_id"] == vk.calls[1]["random_id"]
