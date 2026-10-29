from __future__ import annotations

from datetime import datetime, timezone

from app.textutil import (
    format_money,
    keyword_regex,
    normalize,
    parse_budget,
    parse_rfc822_datetime,
    parse_russian_datetime,
    stem_text,
    truncate,
)


def test_normalize_dashes_and_yo() -> None:
    assert normalize("Telegram-бот, ёлка / API") == "telegram бот елка api"


def test_keyword_word_boundaries() -> None:
    regex = keyword_regex("python")
    assert regex.search(normalize("нужен Python-разработчик"))
    assert not regex.search(normalize("pythonista forever"))


def test_keyword_matches_russian_inflections() -> None:
    assert keyword_regex("скрипт").search(stem_text("Разработка скрипта и парсера"))
    assert keyword_regex("парсер").search(stem_text("нужен парсер Авито"))
    assert keyword_regex("автоматизация").search(stem_text("задачи автоматизации"))
    assert not keyword_regex("бот").search(stem_text("ботаника для сада"))


def test_parse_budget_fixed() -> None:
    assert parse_budget("25 000 ₽ / заказ") == (25000, 25000, "project")
    assert parse_budget("Бюджет: 15 000  ₽") == (15000, 15000, "project")


def test_parse_budget_range_and_open() -> None:
    assert parse_budget("50 000 - 80 000 ₽") == (50000, 80000, "project")
    assert parse_budget("от 30 000 ₽") == (30000, None, "project")
    assert parse_budget("до 40 000 ₽") == (None, 40000, "project")


def test_parse_budget_hourly() -> None:
    assert parse_budget("1 500 ₽/час") == (1500, 1500, "hour")


def test_parse_budget_negotiable() -> None:
    assert parse_budget("по договоренности") == (None, None, "unknown")
    assert parse_budget("") == (None, None, "unknown")


def test_dates() -> None:
    dt = parse_russian_datetime("29.10.2026 10:50")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).hour == 7

    rfc = parse_rfc822_datetime("Thu, 29 Oct 2026 09:05:18 GMT")
    assert rfc is not None and rfc.year == 2026


def test_format_and_truncate() -> None:
    assert format_money(25000) == "25 000 ₽"
    assert format_money(1500, "hour") == "1 500 ₽/час"
    assert truncate("x" * 100, 10).endswith("…")
