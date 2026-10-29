from __future__ import annotations

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

    notifier = Notifier(load_settings(), DummyDb(), None)  # type: ignore[arg-type]
    assert asyncio.run(notifier.deliver_pending()) == 0
