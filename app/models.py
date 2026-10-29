from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ParsedOrder:
    source: str
    external_id: str
    url: str
    title: str
    description: str = ""
    budget_text: str = ""
    budget_min: int | None = None
    budget_max: int | None = None
    budget_unit: str = "unknown"  # project | hour | unknown
    category: str = ""
    published_at: datetime | None = None
    fetched_at: datetime = field(default_factory=utcnow)
    score: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    llm_reason: str = ""

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.source, self.external_id)

    @property
    def budget_display(self) -> str:
        if not self.budget_text:
            return ""
        return self.budget_text.strip()


@dataclass(slots=True)
class ScoreResult:
    score: int
    matched: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class DeliveryTarget:
    peer_id: int
    kind: str  # chat | user
    user_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"peer_id": self.peer_id, "kind": self.kind, "user_id": self.user_id}
