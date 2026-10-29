"""Re-score existing orders with the current keyword/stopword rules.

Usage (inside the container):
    python -m scripts.rescore
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from app.config import load_settings
from app.db import Database
from app.filters import OrderFilter
from app.models import ParsedOrder
from app.skills import SkillCatalog


def row_to_order(row: dict) -> ParsedOrder:
    published = None
    if row.get("published_at"):
        try:
            published = datetime.fromisoformat(row["published_at"])
        except ValueError:
            published = None
    return ParsedOrder(
        source=row["source"],
        external_id=row["external_id"],
        url=row["url"],
        title=row["title"],
        description=row.get("description") or "",
        budget_text=row.get("budget_text") or "",
        budget_min=row.get("budget_min"),
        budget_max=row.get("budget_max"),
        budget_unit=row.get("budget_unit") or "unknown",
        category=row.get("category") or "",
        published_at=published,
    )


async def main() -> int:
    settings = load_settings()
    db = Database(settings.database_path)
    await db.init()
    catalog = SkillCatalog(settings, db)
    await catalog.ensure_backfilled()
    await catalog.refresh()
    order_filter = OrderFilter(settings, catalog.keywords, catalog.disabled_stopwords)
    try:
        assert db._conn is not None  # noqa: SLF001 - internal maintenance script
        rows = [dict(row) for row in db._conn.execute("SELECT * FROM orders").fetchall()]
        passed = 0
        for row in rows:
            order = row_to_order(row)
            result = order_filter.score(order)
            old_score = int(row.get("score") or 0)
            # Keep a stronger LLM-assisted score and its explanation.
            order.score = max(old_score, result.score)
            if result.score > old_score:
                order.matched_keywords = result.matched
                order.red_flags = result.red_flags
                order.llm_reason = result.reason
            else:
                try:
                    order.matched_keywords = json.loads(row.get("matched_keywords") or "[]")
                except json.JSONDecodeError:
                    order.matched_keywords = []
                try:
                    order.red_flags = json.loads(row.get("red_flags") or "[]")
                except json.JSONDecodeError:
                    order.red_flags = []
                order.llm_reason = str(row.get("llm_reason") or "")
            await db.update_order_analysis(int(row["id"]), order)
            if order.score >= settings.min_score:
                passed += 1
        print(
            f"rescored={len(rows)} passed={passed} min_score={settings.min_score} "
            f"skills={len(catalog.skills)}"
        )
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
