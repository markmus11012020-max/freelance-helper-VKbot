from __future__ import annotations

import re
from datetime import datetime, timezone

from app.config import Settings
from app.models import ParsedOrder, ScoreResult
from app.textutil import format_money, keyword_regex, parse_budget, stem_text, truncate

_HARD_RED_FLAGS = (
    "бесплатно",
    "без оплаты",
    "безвозмездно",
    "оплата после",
    "оплата по результатам",
    "работа за отзыв",
    "за тестовое",
)


class OrderFilter:
    """Rule-based prefilter and scorer.

    Cheap deterministic pass over every incoming order. The LLM re-ranker is
    applied later, only to candidates that survive this stage.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._keywords = [(kw, keyword_regex(kw)) for kw in settings.keywords]
        self._stopwords = [(sw, keyword_regex(sw)) for sw in settings.stopwords]

    def prefilter(self, order: ParsedOrder) -> bool:
        haystack = stem_text(f"{order.title} {order.description}")
        if not haystack:
            return False
        if any(regex.search(haystack) for _, regex in self._stopwords):
            # Still allow orders that have strong keywords and no explicit
            # stopword in the title: the full scorer decides.
            title = stem_text(order.title)
            return any(regex.search(title) for _, regex in self._keywords)
        return any(regex.search(haystack) for _, regex in self._keywords)

    def score(self, order: ParsedOrder) -> ScoreResult:
        title = stem_text(order.title)
        haystack = stem_text(f"{order.title} {order.description}")

        matched = [kw for kw, regex in self._keywords if regex.search(haystack)]
        title_matched = [kw for kw, regex in self._keywords if regex.search(title)]
        stop_hits = [sw for sw, regex in self._stopwords if regex.search(haystack)]

        budget_min = order.budget_min
        budget_max = order.budget_max
        unit = order.budget_unit
        if budget_min is None and order.budget_text:
            budget_min, budget_max, unit = parse_budget(order.budget_text)
            order.budget_min, order.budget_max, order.budget_unit = budget_min, budget_max, unit

        score = 0
        score += min(48, 12 * len(matched))
        if title_matched:
            score += 8

        if unit == "hour" and budget_min:
            if budget_min >= 2000:
                score += 28
            elif budget_min >= 1500:
                score += 24
            elif budget_min >= 1000:
                score += 20
            elif budget_min >= 700:
                score += 12
            else:
                score += 4
        elif budget_min:
            if budget_min >= 50_000:
                score += 28
            elif budget_min >= 30_000:
                score += 26
            elif budget_min >= self.settings.budget_min_rub:
                score += 22
            elif budget_min >= 7_000:
                score += 12
            else:
                score += 4

        published = order.published_at
        if published is not None:
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
            if age_hours <= 2:
                score += 10
            elif age_hours <= 8:
                score += 6
            elif age_hours <= 24:
                score += 3
            elif age_hours <= 72:
                score += 1
        else:
            score += 2

        score += 3  # both sources are professional marketplaces
        if stop_hits:
            score -= min(60, 30 * len(stop_hits))

        red_flags = [flag for flag in _HARD_RED_FLAGS if flag in haystack]
        red_flags.extend(f"стоп-слово: {sw}" for sw in stop_hits)
        if red_flags:
            score -= min(45, 15 * len(red_flags))

        score = max(0, min(100, score))

        details: list[str] = []
        if matched:
            details.append("совпало: " + ", ".join(matched[:5]))
        if budget_min:
            details.append("бюджет: " + format_money(budget_min, unit))
        else:
            details.append("бюджет не указан")
        if stop_hits:
            details.append("минусы: " + ", ".join(stop_hits[:3]))

        return ScoreResult(
            score=score,
            matched=matched,
            red_flags=red_flags,
            reason=truncate("; ".join(details), 300),
        )
