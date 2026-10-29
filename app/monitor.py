from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.db import Database
from app.filters import OrderFilter
from app.llm import LlmClient
from app.models import ParsedOrder
from app.notifier import Notifier
from app.parsers.base import BaseParser, FetchError
from app.textutil import truncate

log = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        parsers: list[BaseParser],
        order_filter: OrderFilter,
        llm: LlmClient,
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.db = db
        self.parsers = {parser.source: parser for parser in parsers}
        self.order_filter = order_filter
        self.llm = llm
        self.notifier = notifier

    async def run_forever(self) -> None:
        await self.scan_once()
        while True:
            try:
                if await self.db.kv_get("paused", "0") == "1":
                    await asyncio.sleep(30)
                    continue
                await asyncio.sleep(self.settings.scan_interval_seconds)
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor cycle failed; retrying in 60s")
                await asyncio.sleep(60)

    async def scan_once(self) -> dict[str, int]:
        if await self.db.kv_get("paused", "0") == "1":
            log.info("monitor is paused, skip scan")
            return {"fetched": 0, "candidates": 0, "new": 0, "sent": 0}

        fetched = 0
        raw_orders: list[ParsedOrder] = []
        for source, parser in self.parsers.items():
            try:
                orders = await parser.fetch()
            except FetchError as exc:
                fail_count = await self.db.source_fail(source, str(exc))
                log.warning("source %s failed (%s): %s", source, fail_count, exc)
                if fail_count == 3:
                    await self.notifier.notify_admins(
                        f"⚠️ Источник {source} не отвечает 3 цикла подряд.\n{truncate(str(exc), 300)}"
                    )
                continue
            except Exception as exc:
                await self.db.source_fail(source, str(exc))
                log.exception("source %s crashed", source)
                continue
            await self.db.source_ok(source)
            fetched += len(orders)
            raw_orders.extend(orders)

        candidates = [order for order in raw_orders if self.order_filter.prefilter(order)]

        # Only unseen orders are enriched and sent to the LLM. This keeps
        # polite request rates and avoids paying for the same order every cycle.
        fresh: list[ParsedOrder] = []
        for order in candidates:
            if not await self.db.order_exists(order.source, order.external_id):
                fresh.append(order)

        enriched = 0
        for order in fresh:
            if enriched >= self.settings.fl_ru_max_detail_fetch:
                break
            if order.budget_min is not None:
                continue
            parser = self.parsers.get(order.source)
            if parser is None:
                continue
            try:
                await parser.enrich(order)
                enriched += 1
                await asyncio.sleep(1.0)
            except Exception:
                log.exception("enrich failed for %s", order.url)

        rules = {}
        for order in fresh:
            result = self.order_filter.score(order)
            rules[id(order)] = result
            order.score = result.score
            order.matched_keywords = result.matched
            order.red_flags = result.red_flags
            order.llm_reason = result.reason

        # LLM re-ranks only the most promising new orders of this cycle.
        if self.llm.available:
            top_for_llm = sorted(fresh, key=lambda item: item.score, reverse=True)[
                : max(0, self.settings.max_orders_per_cycle)
            ]
            for order in top_for_llm:
                if order.score < 35:
                    continue
                try:
                    data = await self.llm.score_order(order, rules[id(order)])
                except Exception:
                    log.exception("LLM scoring failed for %s", order.external_id)
                    continue
                if not data:
                    continue
                rule_score = rules[id(order)].score
                order.score = max(0, min(100, round(0.6 * int(data["score"]) + 0.4 * rule_score)))
                order.llm_reason = str(data.get("reason") or order.llm_reason)[:400]
                extra_flags = [flag for flag in data.get("red_flags") or [] if flag not in order.red_flags]
                order.red_flags = (order.red_flags + extra_flags)[:8]

        new_orders: list[tuple[int, ParsedOrder]] = []
        for order in fresh:
            order_id, is_new = await self.db.upsert_order(order)
            await self.db.update_order_analysis(order_id, order)
            if is_new:
                new_orders.append((order_id, order))

        targets = await self.db.delivery_targets()
        selected = [
            (order_id, order)
            for order_id, order in new_orders
            if order.score >= self.settings.min_score
        ]
        selected.sort(key=lambda pair: pair[1].score, reverse=True)
        selected = selected[: self.settings.max_orders_per_cycle]

        sent = 0
        if self.settings.dry_run:
            for order_id, order in selected:
                log.info(
                    "DRY-RUN order %s score=%s source=%s\n%s",
                    order_id,
                    order.score,
                    order.source,
                    self.notifier.build_card(self._card_row(order_id, order)),
                )
        else:
            for order_id, order in selected:
                for target in targets:
                    await self.db.enqueue_delivery(order_id, int(target["peer_id"]))
            if selected and not targets:
                log.warning("no delivery targets yet: send /connect in a VK chat")
            sent = await self.notifier.deliver_pending()

        await self.db.kv_set("last_scan", datetime.now(timezone.utc).isoformat())
        summary = {
            "fetched": fetched,
            "candidates": len(candidates),
            "new": len(new_orders),
            "sent": sent,
        }
        log.info("scan done: %s", summary)
        return summary

    @staticmethod
    def _card_row(order_id: int, order: ParsedOrder) -> dict:
        return {
            "id": order_id,
            "order_id": order_id,
            "score": order.score,
            "source": order.source,
            "title": order.title,
            "url": order.url,
            "description": order.description,
            "budget_text": order.budget_text,
            "budget_min": order.budget_min,
            "budget_unit": order.budget_unit,
            "category": order.category,
            "published_at": order.published_at.isoformat() if order.published_at else None,
            "llm_reason": order.llm_reason,
            "matched_keywords": json.dumps(order.matched_keywords, ensure_ascii=False),
        }
