from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import load_settings
from app.filters import OrderFilter
from app.models import ParsedOrder


def make_order(**kwargs) -> ParsedOrder:
    defaults = dict(
        source="fl.ru",
        external_id="1",
        url="https://example.com/1",
        title="Нужен парсер Avito на Python",
        description="Собрать данные по объявлениям, выгрузка в Google Sheets.",
        budget_text="30 000 ₽",
        budget_min=30000,
        budget_max=30000,
        budget_unit="project",
        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    defaults.update(kwargs)
    return ParsedOrder(**defaults)


def test_good_order_scores_high() -> None:
    flt = OrderFilter(load_settings())
    order = make_order()
    result = flt.score(order)
    assert result.score >= 60
    assert "парсер" in result.matched
    assert flt.prefilter(order)


def test_spam_scores_low() -> None:
    flt = OrderFilter(load_settings())
    order = make_order(
        title="Доработка сайта на WordPress",
        description="Нужен SEO-специалист, правки в вёрстке, 1С-интеграция.",
        budget_text="5 000 ₽",
        budget_min=5000,
        budget_unit="project",
    )
    result = flt.score(order)
    assert result.score < 40
    assert result.red_flags


def test_old_cheap_order_is_filtered() -> None:
    flt = OrderFilter(load_settings())
    order = make_order(
        title="Скрипт",
        description="Небольшой скрипт",
        budget_text="",
        budget_min=None,
        budget_max=None,
        budget_unit="unknown",
        published_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    assert flt.score(order).score < load_settings().min_score


def test_sales_script_is_not_a_dev_order() -> None:
    flt = OrderFilter(load_settings())
    order = make_order(
        title="Обзвон базы потенциальных клиентов",
        description="Нужен скрипт обзвона и холодные звонки, выгрузка в Google Sheets.",
        budget_text="15 000 ₽",
        budget_min=15000,
        budget_unit="project",
    )
    assert flt.score(order).score < load_settings().min_score


def test_prefilter_rejects_unrelated() -> None:
    flt = OrderFilter(load_settings())
    order = make_order(
        title="Нужен монтаж видео",
        description="Смонтировать ролик для соцсетей, цветокоррекция.",
    )
    assert not flt.prefilter(order)
