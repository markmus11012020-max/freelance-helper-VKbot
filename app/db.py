from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.models import ParsedOrder, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    budget_text TEXT NOT NULL DEFAULT '',
    budget_min INTEGER,
    budget_max INTEGER,
    budget_unit TEXT NOT NULL DEFAULT 'unknown',
    category TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    matched_keywords TEXT NOT NULL DEFAULT '[]',
    red_flags TEXT NOT NULL DEFAULT '[]',
    llm_reason TEXT NOT NULL DEFAULT '',
    card_text TEXT NOT NULL DEFAULT '',
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_score ON orders(score DESC, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_fetched ON orders(fetched_at DESC);

CREATE TABLE IF NOT EXISTS peers (
    peer_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'user',
    user_id INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT NOT NULL DEFAULT '',
    added_by INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    peer_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(order_id, peer_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, id);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT PRIMARY KEY,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    ok INTEGER NOT NULL DEFAULT 1,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_ok TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    alerted_at TEXT
);
"""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class Database:
    """Single-connection SQLite wrapper.

    All access is serialized with an asyncio lock: the bot has two long-running
    loops (monitor and VK long poll), and SQLite does not like concurrent
    writers from different tasks.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        assert self._conn is not None, "database is not initialized"
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ---------- orders ----------

    async def upsert_order(self, order: ParsedOrder) -> tuple[int, bool]:
        async with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "SELECT id FROM orders WHERE source = ? AND external_id = ?",
                order.dedup_key,
            )
            row = cur.fetchone()
            if row is not None:
                return int(row["id"]), False
            cur = self._conn.execute(
                """
                INSERT INTO orders (
                    source, external_id, url, title, description, budget_text,
                    budget_min, budget_max, budget_unit, category, published_at,
                    fetched_at, score, matched_keywords, red_flags, llm_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.source,
                    order.external_id,
                    order.url,
                    order.title,
                    order.description,
                    order.budget_text,
                    order.budget_min,
                    order.budget_max,
                    order.budget_unit,
                    order.category,
                    _iso(order.published_at),
                    _iso(order.fetched_at),
                    order.score,
                    json.dumps(order.matched_keywords, ensure_ascii=False),
                    json.dumps(order.red_flags, ensure_ascii=False),
                    order.llm_reason,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid), True

    async def update_order_analysis(self, order_id: int, order: ParsedOrder) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    UPDATE orders SET score = ?, matched_keywords = ?, red_flags = ?,
                        llm_reason = ?, budget_text = ?, budget_min = ?, budget_max = ?,
                        budget_unit = ?
                    WHERE id = ?
                    """,
                    (
                        order.score,
                        json.dumps(order.matched_keywords, ensure_ascii=False),
                        json.dumps(order.red_flags, ensure_ascii=False),
                        order.llm_reason,
                        order.budget_text,
                        order.budget_min,
                        order.budget_max,
                        order.budget_unit,
                        order_id,
                    ),
                )

    async def set_card_text(self, order_id: int, text: str) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute("UPDATE orders SET card_text = ? WHERE id = ?", (text, order_id))

    async def order_exists(self, source: str, external_id: str) -> bool:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT 1 FROM orders WHERE source = ? AND external_id = ? LIMIT 1",
                (source, external_id),
            ).fetchone()
        return row is not None

    async def get_order(self, order_id: int) -> dict[str, Any] | None:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    async def recent_orders(self, limit: int = 10, min_score: int | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            if min_score is None:
                rows = self._conn.execute(
                    "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM orders WHERE score >= ? ORDER BY id DESC LIMIT ?",
                    (min_score, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    async def orders_for_backfill(self, limit: int = 10, min_score: int = 45) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                """
                SELECT * FROM orders
                WHERE score >= ?
                ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def orders_since(self, since_iso: str, min_score: int = 0) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE fetched_at >= ? AND score >= ? ORDER BY score DESC",
                (since_iso, min_score),
            ).fetchall()
        return [dict(row) for row in rows]

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            assert self._conn is not None
            total = self._conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
            today = self._conn.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE date(fetched_at) = date('now')"
            ).fetchone()["c"]
            sent = self._conn.execute(
                "SELECT COUNT(*) AS c FROM deliveries WHERE status = 'sent'"
            ).fetchone()["c"]
            pending = self._conn.execute(
                "SELECT COUNT(*) AS c FROM deliveries WHERE status = 'pending'"
            ).fetchone()["c"]
            last = self._conn.execute(
                "SELECT MAX(fetched_at) AS ts FROM orders"
            ).fetchone()["ts"]
        return {"total": total, "today": today, "sent": sent, "pending": pending, "last_order": last}

    # ---------- peers ----------

    async def upsert_peer(
        self,
        peer_id: int,
        kind: str,
        user_id: int = 0,
        role: str = "member",
        status: str = "active",
        title: str = "",
        added_by: int = 0,
    ) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO peers (peer_id, kind, user_id, role, status, title, added_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(peer_id) DO UPDATE SET
                        kind = excluded.kind,
                        user_id = CASE WHEN excluded.user_id != 0 THEN excluded.user_id ELSE peers.user_id END,
                        role = CASE WHEN excluded.role != 'member' THEN excluded.role ELSE peers.role END,
                        status = excluded.status,
                        title = CASE WHEN excluded.title != '' THEN excluded.title ELSE peers.title END
                    """,
                    (peer_id, kind, user_id, role, status, title, added_by, _iso(utcnow())),
                )

    async def get_peer(self, peer_id: int) -> dict[str, Any] | None:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
        return dict(row) if row else None

    async def list_peers(self, status: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            if status is None:
                rows = self._conn.execute("SELECT * FROM peers ORDER BY created_at").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM peers WHERE status = ? ORDER BY created_at", (status,)
                ).fetchall()
        return [dict(row) for row in rows]

    async def delivery_targets(self) -> list[dict[str, Any]]:
        """Active chats; if none, fall back to active admin DMs."""
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM peers WHERE status = 'active' AND kind = 'chat' ORDER BY created_at"
            ).fetchall()
            if not rows:
                rows = self._conn.execute(
                    "SELECT * FROM peers WHERE status = 'active' AND kind = 'user' AND role = 'admin'"
                ).fetchall()
        return [dict(row) for row in rows]

    async def add_chat_user(self, peer_id: int, user_id: int) -> None:
        await self.upsert_peer(peer_id, kind="chat", user_id=user_id, role="member", status="active")

    # ---------- deliveries ----------

    async def enqueue_delivery(self, order_id: int, peer_id: int) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO deliveries (order_id, peer_id, status, created_at) VALUES (?, ?, 'pending', ?)",
                    (order_id, peer_id, _iso(utcnow())),
                )

    async def pending_deliveries(self, limit: int = 30, max_attempts: int = 3) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                """
                SELECT d.*, o.title, o.url, o.description, o.category, o.budget_text,
                       o.budget_min, o.budget_max, o.budget_unit, o.llm_reason,
                       o.matched_keywords, o.source, o.card_text, o.published_at, o.score
                FROM deliveries d JOIN orders o ON o.id = d.order_id
                WHERE d.status IN ('pending', 'failed') AND d.attempts < ?
                ORDER BY d.id LIMIT ?
                """,
                (max_attempts, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def mark_delivery(self, delivery_id: int, status: str, error: str = "") -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    UPDATE deliveries SET status = ?, error = ?, attempts = attempts + 1,
                        sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END
                    WHERE id = ?
                    """,
                    (status, error[:500], status, _iso(utcnow()), delivery_id),
                )

    # ---------- kv ----------

    async def kv_get(self, key: str, default: str = "") -> str:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    async def kv_set(self, key: str, value: str) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # ---------- llm ----------

    async def llm_cache_get(self, key: str) -> str | None:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT response FROM llm_cache WHERE key = ?", (key,)
            ).fetchone()
        return str(row["response"]) if row else None

    async def llm_cache_put(self, key: str, response: str) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, response, created_at) VALUES (?, ?, ?)",
                    (key, response, _iso(utcnow())),
                )

    async def llm_usage_add(self, day: str, prompt_tokens: int, completion_tokens: int) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO llm_usage (day, prompt_tokens, completion_tokens, calls)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(day) DO UPDATE SET
                        prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                        completion_tokens = completion_tokens + excluded.completion_tokens,
                        calls = calls + 1
                    """,
                    (day, prompt_tokens, completion_tokens),
                )

    async def llm_usage_get(self, day: str) -> dict[str, int]:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute("SELECT * FROM llm_usage WHERE day = ?", (day,)).fetchone()
        if not row:
            return {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        return dict(row)  # type: ignore[return-value]

    # ---------- source health ----------

    async def source_ok(self, source: str) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO source_state (source, ok, fail_count, last_ok, last_error)
                    VALUES (?, 1, 0, ?, '')
                    ON CONFLICT(source) DO UPDATE SET ok = 1, fail_count = 0, last_ok = excluded.last_ok
                    """,
                    (source, _iso(utcnow())),
                )

    async def source_fail(self, source: str, error: str) -> int:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT fail_count FROM source_state WHERE source = ?", (source,)
            ).fetchone()
            fail_count = (int(row["fail_count"]) if row else 0) + 1
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO source_state (source, ok, fail_count, last_error)
                    VALUES (?, 0, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        ok = 0, fail_count = excluded.fail_count, last_error = excluded.last_error
                    """,
                    (source, fail_count, error[:500]),
                )
        return fail_count

    async def sources(self) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute("SELECT * FROM source_state ORDER BY source").fetchall()
        return [dict(row) for row in rows]
