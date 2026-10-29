from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from app.models import ParsedOrder
from app.parsers.base import BaseParser, HttpClient, _clean
from app.textutil import parse_budget, parse_russian_datetime

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"/task/view/(\d+)")
_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?")
_BASE = "https://freelance.ru"


def parse_listing(html: str) -> list[ParsedOrder]:
    """Parse a freelance.ru task feed page. Pure function, used by tests."""
    soup = BeautifulSoup(html, "lxml")
    orders: list[ParsedOrder] = []

    for card in soup.select("article.task-card"):
        link = card.select_one("a.task-card__title-link")
        if link is None:
            continue
        href = link.get("href") or ""
        match = _ID_RE.search(href)
        if not match:
            continue

        budget_el = card.select_one(".task-card__budget")
        budget_text = _clean(budget_el.get_text(" ", strip=True)) if budget_el else ""
        budget_min, budget_max, unit = parse_budget(budget_text)

        published = None
        for foot in card.select(".task-card__foot-item[title]"):
            found = _DATE_RE.search(foot.get("title") or "")
            if found:
                published = parse_russian_datetime(found.group())
                break
        if published is None:
            time_el = card.select_one("time[datetime]")
            if time_el is not None:
                published = parse_russian_datetime(time_el.get("datetime") or "")

        category_el = card.select_one(".task-chip--cat")

        orders.append(
            ParsedOrder(
                source="freelance.ru",
                external_id=match.group(1),
                url=_BASE + href if href.startswith("/") else href,
                title=_clean(link.get_text(" ", strip=True)),
                description=_clean(
                    card.select_one(".task-card__desc").get_text(" ", strip=True)
                    if card.select_one(".task-card__desc")
                    else ""
                ),
                budget_text=budget_text,
                budget_min=budget_min,
                budget_max=budget_max,
                budget_unit=unit,
                category=_clean(category_el.get_text(" ", strip=True)) if category_el else "",
                published_at=published,
            )
        )
    return orders


class FreelanceRuParser(BaseParser):
    source = "freelance.ru"

    def _page_url(self, page: int) -> str:
        return f"{_BASE}/task?page={page}"

    async def fetch(self) -> list[ParsedOrder]:
        pages = max(1, self.settings.freelance_ru_pages)
        orders: list[ParsedOrder] = []
        for page in range(1, pages + 1):
            html = await self.http.text(self._page_url(page))
            orders.extend(parse_listing(html))
            if page < pages:
                import asyncio

                await asyncio.sleep(1.0)  # be polite to the source
        log.info("freelance.ru: fetched %s orders from %s page(s)", len(orders), pages)
        return self._dedupe(orders)
