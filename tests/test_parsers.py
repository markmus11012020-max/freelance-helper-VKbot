from __future__ import annotations

from pathlib import Path

from app.parsers.fl_ru import extract_budget, parse_rss
from app.parsers.freelance_ru import parse_listing

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_fl_rss() -> None:
    xml = (FIXTURES / "fl_ru_rss.xml").read_text(encoding="utf-8")
    orders = parse_rss(xml)
    assert len(orders) >= 50
    assert all(order.source == "fl.ru" for order in orders)
    assert all(order.external_id.isdigit() for order in orders)
    assert all(order.url.startswith("https://www.fl.ru/projects/") for order in orders)
    # RSS may already contain budget in the title like "(Бюджет: 15 000 ₽)".
    assert any(order.budget_min == 15000 for order in orders)
    # Budget marker must be stripped from the visible title.
    assert all("Бюджет:" not in order.title for order in orders)


def test_extract_budget_from_project_page() -> None:
    html = (FIXTURES / "fl_ru_project.html").read_text(encoding="utf-8", errors="replace")
    assert extract_budget(html) == "по договоренности"


def test_parse_freelance_listing() -> None:
    html = (FIXTURES / "freelance_ru_listing.html").read_text(encoding="utf-8")
    orders = parse_listing(html)
    assert len(orders) >= 20
    assert all(order.source == "freelance.ru" for order in orders)
    assert all(order.external_id.isdigit() for order in orders)
    assert all(order.url.startswith("https://freelance.ru/task/view/") for order in orders)
    assert any(order.budget_min and order.budget_min >= 10_000 for order in orders)
    assert any(order.published_at is not None for order in orders)
