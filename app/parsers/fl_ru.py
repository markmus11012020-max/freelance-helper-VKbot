from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from app.models import ParsedOrder
from app.parsers.base import BaseParser, FetchError, HttpClient, _clean
from app.textutil import parse_budget, parse_rfc822_datetime

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"/projects/(\d+)")
_TITLE_BUDGET_RE = re.compile(r"\s*\(Бюджет:\s*([^)]+)\)\s*", re.IGNORECASE)


def parse_rss(xml_text: str) -> list[ParsedOrder]:
    """Parse FL.ru RSS feed into orders. Pure function, used by tests."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"invalid RSS: {exc}") from exc

    orders: list[ParsedOrder] = []
    for item in root.findall("./channel/item"):
        title = _clean(item.findtext("title"))
        link = _clean(item.findtext("link"))
        match = _ID_RE.search(link)
        if not title or not link or not match:
            continue

        budget_text = ""
        title_budget = _TITLE_BUDGET_RE.search(title)
        if title_budget:
            budget_text = _clean(title_budget.group(1)).split(",")[0].strip()
            title = _clean(_TITLE_BUDGET_RE.sub(" ", title))

        budget_min, budget_max, unit = parse_budget(budget_text)
        orders.append(
            ParsedOrder(
                source="fl.ru",
                external_id=match.group(1),
                url=link,
                title=title,
                description=_clean(item.findtext("description")),
                budget_text=budget_text,
                budget_min=budget_min,
                budget_max=budget_max,
                budget_unit=unit,
                category=_clean(item.findtext("category")),
                published_at=parse_rfc822_datetime(item.findtext("pubDate") or ""),
            )
        )
    return orders


def extract_budget(html: str) -> str:
    """Extract 'Бюджет: ...' value from an FL.ru project page."""
    soup = BeautifulSoup(html, "lxml")
    node = soup.find(string=re.compile(r"Бюджет\s*:", re.IGNORECASE))
    if node is None or node.parent is None:
        return ""
    text = _clean(node.parent.get_text(" ", strip=True))
    match = re.search(r"Бюджет\s*:\s*(.+)$", text, re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip()
    # Cut trailing UI noise if the parent block is wider than the budget line.
    value = re.split(r"\s{2,}|Ссылка на заказ|Откликнуться", value)[0]
    return _clean(value)[:120]


class FlRuParser(BaseParser):
    source = "fl.ru"

    async def fetch(self) -> list[ParsedOrder]:
        xml_text = await self.http.text(self.settings.fl_ru_rss_url)
        orders = parse_rss(xml_text)
        log.info("fl.ru: fetched %s orders from RSS", len(orders))
        return self._dedupe(orders)

    async def enrich(self, order: ParsedOrder) -> None:
        if order.budget_min is not None or not order.url:
            return
        try:
            html = await self.http.text(order.url)
        except FetchError as exc:
            log.warning("fl.ru: budget fetch failed for %s: %s", order.url, exc)
            return
        budget_text = extract_budget(html)
        if not budget_text:
            return
        order.budget_text = budget_text
        order.budget_min, order.budget_max, order.budget_unit = parse_budget(budget_text)
