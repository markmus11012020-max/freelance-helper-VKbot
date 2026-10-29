from __future__ import annotations

import logging
from typing import Any

from app.models import ParsedOrder
from app.parsers.base import BaseParser, FetchError, HttpClient, _clean
from app.textutil import format_money, parse_russian_datetime

log = logging.getLogger(__name__)

_BASE = "https://kwork.ru/projects"


def parse_projects(payload: dict[str, Any]) -> list[ParsedOrder]:
    """Parse Kwork's internal projects JSON. Pure function, used by tests."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    pagination = data.get("pagination") or {}
    wants = pagination.get("data") or []

    orders: list[ParsedOrder] = []
    for item in wants:
        try:
            external_id = str(int(item.get("id")))
        except (TypeError, ValueError):
            continue
        title = _clean(item.get("name"))
        if not title:
            continue

        price = item.get("priceLimit") or item.get("possiblePriceLimit") or 0
        try:
            price_value = int(float(price))
        except (TypeError, ValueError):
            price_value = 0
        budget_text = format_money(price_value) if price_value > 0 else ""

        orders.append(
            ParsedOrder(
                source="kwork.ru",
                external_id=external_id,
                url=f"{_BASE}/{external_id}/view",
                title=title,
                description=_clean(item.get("description")),
                budget_text=budget_text,
                budget_min=price_value or None,
                budget_max=price_value or None,
                budget_unit="project" if price_value else "unknown",
                category="",
                published_at=parse_russian_datetime(str(item.get("date_create") or "")),
            )
        )
    return orders


class KworkParser(BaseParser):
    source = "kwork.ru"

    async def fetch(self) -> list[ParsedOrder]:
        pages = max(1, self.settings.kwork_pages)
        orders: list[ParsedOrder] = []
        for page in range(1, pages + 1):
            payload = await self.http.post_json(
                self.settings.kwork_url,
                data={"page": page},
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            )
            if not isinstance(payload, dict) or not payload.get("success"):
                raise FetchError(f"kwork: unexpected response on page {page}")
            orders.extend(parse_projects(payload))
            if page < pages:
                import asyncio

                await asyncio.sleep(1.0)
        log.info("kwork.ru: fetched %s orders from %s page(s)", len(orders), pages)
        return self._dedupe(orders)
