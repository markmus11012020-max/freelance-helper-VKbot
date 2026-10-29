from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

import snowballstemmer

_stemmer = snowballstemmer.stemmer("russian")

_DASH_RE = re.compile(r"[\u2010-\u2015_/\\|]+")
_ALLOWED_RE = re.compile(r"[^\w\s+#.]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"\d[\d\s\u00a0]{2,}")

_EMPTY_BUDGET_MARKERS = (
    "договор", "обсужда", "по результатам", "торг", "не указан", "не указана",
)


def normalize(text: str) -> str:
    """Lowercase, unify ё/е and dashes, strip punctuation, collapse spaces."""
    if not text:
        return ""
    text = text.lower().replace("ё", "е")
    text = _DASH_RE.sub(" ", text)
    text = _ALLOWED_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


@lru_cache(maxsize=50_000)
def _stem_word(word: str) -> str:
    return _stemmer.stemWord(word)


def stem_text(text: str) -> str:
    """Normalize and reduce Russian words to stems, keeping the word order."""
    return " ".join(_stem_word(part) for part in normalize(text).split())


def keyword_regex(keyword: str) -> re.Pattern[str]:
    parts = [re.escape(_stem_word(part)) for part in normalize(keyword).split() if part]
    body = r"\s+".join(parts) if parts else re.escape(normalize(keyword))
    return re.compile(rf"(?<![\w]){body}(?![\w])", re.UNICODE)


def _clean_number(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    value = int(digits)
    if value < 100:
        return None
    return value


def parse_budget(text: str) -> tuple[int | None, int | None, str]:
    """Best-effort extraction of (min, max, unit) from a budget string.

    unit is "hour" for rate-based budgets, "project" for fixed prices and
    "unknown" when nothing could be parsed.
    """
    if not text:
        return None, None, "unknown"
    low = text.lower().replace("\u00a0", " ").replace("\u202f", " ")
    if any(marker in low for marker in _EMPTY_BUDGET_MARKERS):
        return None, None, "unknown"

    unit = "unknown"
    if re.search(r"(?:/|\bв\b|\bза\b)\s*(?:час|ч\b|hour)", low):
        unit = "hour"
    elif re.search(r"₽|руб|р\.", low):
        unit = "project"

    numbers: list[tuple[int, int, int]] = []
    for match in _NUMBER_RE.finditer(low):
        value = _clean_number(match.group())
        if value is not None:
            numbers.append((value, match.start(), match.end()))
    if not numbers:
        return None, None, "unknown"

    # Keep only sane values: 300..5_000_000 for hour rates and projects.
    numbers = [(v, s, e) for v, s, e in numbers if 100 <= v <= 5_000_000]
    if not numbers:
        return None, None, "unknown"

    if len(numbers) >= 2:
        first, second = numbers[0], numbers[1]
        between = low[first[2]:second[1]]
        if re.search(r"[-–—]|до|от", between) or "—" in between:
            low_val, high_val = sorted((first[0], second[0]))
            return low_val, high_val, unit

    value = numbers[0][0]
    before = low[max(0, numbers[0][1] - 12):numbers[0][1]]
    if re.search(r"\bдо\s*$", before):
        return None, value, unit
    if re.search(r"\bот\s*$", before):
        return value, None, unit
    return value, value, unit


def parse_russian_datetime(raw: str) -> datetime | None:
    """Parse '29.10.2026 10:50' (assumed Moscow time) into aware UTC datetime."""
    raw = (raw or "").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        from zoneinfo import ZoneInfo

        return dt.replace(tzinfo=ZoneInfo("Europe/Moscow")).astimezone(timezone.utc)
    return None


def parse_rfc822_datetime(raw: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_money(value: int | None, unit: str = "project") -> str:
    if value is None:
        return "—"
    formatted = f"{value:,}".replace(",", " ")
    suffix = " ₽/час" if unit == "hour" else " ₽"
    return formatted + suffix


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix
