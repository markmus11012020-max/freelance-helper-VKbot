from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS repo_sources (
    repo_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    default_branch TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]',
    pushed_at TEXT,
    size_kb INTEGER NOT NULL DEFAULT 0,
    fork INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    commit_sha TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    last_error TEXT NOT NULL DEFAULT '',
    chunks INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repo_sources_status ON repo_sources(status);

CREATE TABLE IF NOT EXISTS repo_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    repo_full_name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'code',
    path TEXT NOT NULL DEFAULT '',
    chunk_no INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    line_start INTEGER,
    line_end INTEGER,
    commit_sha TEXT NOT NULL DEFAULT '',
    token_count INTEGER NOT NULL DEFAULT 0,
    embedding BLOB,
    embedding_dim INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(repo_id, path, chunk_no, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_repo_documents_repo ON repo_documents(repo_id);
CREATE INDEX IF NOT EXISTS idx_repo_documents_kind ON repo_documents(kind);

CREATE TABLE IF NOT EXISTS order_meta (
    order_id INTEGER PRIMARY KEY,
    client_name TEXT NOT NULL DEFAULT '',
    name_source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    peer_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    order_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_messages_scope ON memory_messages(scope, id DESC);
CREATE INDEX IF NOT EXISTS idx_memory_messages_expires ON memory_messages(expires_at);

CREATE TABLE IF NOT EXISTS user_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fact TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, fact)
);
CREATE INDEX IF NOT EXISTS idx_user_memory_user ON user_memory(user_id, id DESC);

CREATE TABLE IF NOT EXISTS embedding_usage (
    day TEXT PRIMARY KEY,
    tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0
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

    # ---------- repository knowledge ----------

    async def repo_upsert(self, repo: dict[str, Any]) -> None:
        topics = repo.get("topics") or []
        if not isinstance(topics, str):
            topics = json.dumps(list(topics), ensure_ascii=False)
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO repo_sources (
                        repo_id, full_name, url, description, default_branch, language,
                        topics, pushed_at, size_kb, fork, archived, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo_id) DO UPDATE SET
                        full_name = excluded.full_name,
                        url = excluded.url,
                        description = excluded.description,
                        default_branch = excluded.default_branch,
                        language = excluded.language,
                        topics = excluded.topics,
                        pushed_at = excluded.pushed_at,
                        size_kb = excluded.size_kb,
                        fork = excluded.fork,
                        archived = excluded.archived,
                        last_error = ''
                    """,
                    (
                        int(repo["repo_id"]),
                        str(repo.get("full_name") or ""),
                        str(repo.get("url") or ""),
                        str(repo.get("description") or ""),
                        str(repo.get("default_branch") or ""),
                        str(repo.get("language") or ""),
                        topics,
                        repo.get("pushed_at"),
                        int(repo.get("size_kb") or 0),
                        1 if repo.get("fork") else 0,
                        1 if repo.get("archived") else 0,
                        _iso(utcnow()),
                    ),
                )

    async def repo_get(self, repo_id: int) -> dict[str, Any] | None:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM repo_sources WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        return dict(row) if row else None

    async def repo_get_by_name(self, full_name: str) -> dict[str, Any] | None:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM repo_sources WHERE full_name = ?", (full_name,)
            ).fetchone()
        return dict(row) if row else None

    async def repo_list(self, status: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            if status is None:
                rows = self._conn.execute("SELECT * FROM repo_sources ORDER BY full_name").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM repo_sources WHERE status = ? ORDER BY full_name", (status,)
                ).fetchall()
        return [dict(row) for row in rows]

    async def repo_set_status(self, repo_id: int, status: str, error: str = "") -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "UPDATE repo_sources SET status = ?, last_error = ? WHERE repo_id = ?",
                    (status, error[:1000], repo_id),
                )

    async def repo_set_indexed(
        self,
        repo_id: int,
        commit_sha: str,
        summary: str,
        chunks: int,
        *,
        status: str = "indexed",
    ) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    UPDATE repo_sources SET status = ?, commit_sha = ?,
                        summary = ?, chunks = ?, last_error = '', last_indexed_at = ?
                    WHERE repo_id = ?
                    """,
                    (status, commit_sha[:64], summary[:4000], chunks, _iso(utcnow()), repo_id),
                )

    async def repo_replace_documents(
        self, repo_id: int, full_name: str, documents: list[dict[str, Any]]
    ) -> int:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute("DELETE FROM repo_documents WHERE repo_id = ?", (repo_id,))
                for doc in documents:
                    cur.execute(
                        """
                        INSERT INTO repo_documents (
                            repo_id, repo_full_name, kind, path, chunk_no, content,
                            content_hash, url, language, line_start, line_end,
                            commit_sha, token_count, embedding, embedding_dim, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            repo_id,
                            full_name,
                            str(doc.get("kind") or "code"),
                            str(doc.get("path") or ""),
                            int(doc.get("chunk_no") or 0),
                            str(doc.get("content") or ""),
                            str(doc.get("content_hash") or ""),
                            str(doc.get("url") or ""),
                            str(doc.get("language") or ""),
                            doc.get("line_start"),
                            doc.get("line_end"),
                            str(doc.get("commit_sha") or ""),
                            int(doc.get("token_count") or 0),
                            doc.get("embedding"),
                            int(doc.get("embedding_dim") or 0),
                            _iso(utcnow()),
                        ),
                    )
        return len(documents)

    async def repo_documents_all(self, *, with_embedding: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            where = "WHERE embedding IS NOT NULL" if with_embedding else ""
            rows = self._conn.execute(
                f"SELECT * FROM repo_documents {where} ORDER BY repo_id, id"
            ).fetchall()
        return [dict(row) for row in rows]

    async def repo_mark_missing(self, repo_id: int) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute("DELETE FROM repo_documents WHERE repo_id = ?", (repo_id,))
                cur.execute(
                    """
                    UPDATE repo_sources SET status = 'missing', chunks = 0,
                        last_error = 'repository not found in GitHub list'
                    WHERE repo_id = ?
                    """,
                    (repo_id,),
                )

    async def repo_stats(self) -> dict[str, Any]:
        async with self._lock:
            assert self._conn is not None
            total = self._conn.execute("SELECT COUNT(*) AS c FROM repo_sources").fetchone()["c"]
            indexed = self._conn.execute(
                "SELECT COUNT(*) AS c FROM repo_sources WHERE status = 'indexed'"
            ).fetchone()["c"]
            errors = self._conn.execute(
                "SELECT COUNT(*) AS c FROM repo_sources WHERE status = 'error'"
            ).fetchone()["c"]
            partial = self._conn.execute(
                "SELECT COUNT(*) AS c FROM repo_sources WHERE status = 'partial'"
            ).fetchone()["c"]
            chunks = self._conn.execute(
                "SELECT COUNT(*) AS c FROM repo_documents"
            ).fetchone()["c"]
            with_vectors = self._conn.execute(
                "SELECT COUNT(*) AS c FROM repo_documents WHERE embedding IS NOT NULL"
            ).fetchone()["c"]
        return {
            "total": total,
            "indexed": indexed,
            "partial": partial,
            "errors": errors,
            "chunks": chunks,
            "with_vectors": with_vectors,
        }

    # ---------- order metadata ----------

    async def order_client_name_get(self, order_id: int) -> dict[str, Any] | None:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM order_meta WHERE order_id = ?", (order_id,)
            ).fetchone()
        return dict(row) if row else None

    async def order_client_name_set(self, order_id: int, name: str, source: str) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_meta (order_id, client_name, name_source, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        client_name = excluded.client_name,
                        name_source = excluded.name_source,
                        updated_at = excluded.updated_at
                    """,
                    (order_id, name[:80], source[:40], _iso(utcnow())),
                )

    # ---------- short-term memory ----------

    async def memory_add(
        self,
        scope: str,
        *,
        peer_id: int,
        user_id: int,
        role: str,
        content: str,
        order_id: int | None = None,
        ttl_days: int = 7,
        max_messages: int = 20,
    ) -> None:
        now = utcnow()
        expires = now + timedelta(days=max(1, ttl_days))
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_messages WHERE scope = ? AND expires_at <= ?",
                    (scope, _iso(now)),
                )
                cur.execute(
                    """
                    INSERT INTO memory_messages (
                        scope, peer_id, user_id, role, content, order_id, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope,
                        peer_id,
                        user_id,
                        role,
                        content[:4000],
                        order_id,
                        _iso(now),
                        _iso(expires),
                    ),
                )
                cur.execute(
                    """
                    DELETE FROM memory_messages
                    WHERE scope = ? AND id NOT IN (
                        SELECT id FROM memory_messages WHERE scope = ? ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (scope, scope, max(1, max_messages)),
                )

    async def memory_recent(
        self, scope: str, limit: int = 20, now: str | None = None
    ) -> list[dict[str, Any]]:
        now_iso = now or _iso(utcnow())
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM memory_messages
                    WHERE scope = ? AND expires_at > ?
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (scope, now_iso, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    async def memory_clear(self, scope: str) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute("DELETE FROM memory_messages WHERE scope = ?", (scope,))

    async def memory_cleanup(self, now: str | None = None) -> None:
        now_iso = now or _iso(utcnow())
        async with self._lock:
            with self._cursor() as cur:
                cur.execute("DELETE FROM memory_messages WHERE expires_at <= ?", (now_iso,))

    async def memory_count(self, scope: str | None = None) -> int:
        async with self._lock:
            assert self._conn is not None
            if scope is None:
                row = self._conn.execute("SELECT COUNT(*) AS c FROM memory_messages").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_messages WHERE scope = ?", (scope,)
                ).fetchone()
        return int(row["c"])

    # ---------- long-term user memory ----------

    async def user_memory_add(self, user_id: int, fact: str, max_facts: int = 50) -> None:
        fact = " ".join(fact.split())[:1000]
        if not fact:
            return
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO user_memory (user_id, fact, created_at) VALUES (?, ?, ?)",
                    (user_id, fact, _iso(utcnow())),
                )
                cur.execute(
                    """
                    DELETE FROM user_memory
                    WHERE user_id = ? AND id NOT IN (
                        SELECT id FROM user_memory WHERE user_id = ? ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (user_id, user_id, max(1, max_facts)),
                )

    async def user_memory_list(self, user_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM user_memory WHERE user_id = ? ORDER BY id DESC", (user_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    async def user_memory_delete(self, user_id: int, fact_id: int | None = None) -> None:
        async with self._lock:
            with self._cursor() as cur:
                if fact_id is None:
                    cur.execute("DELETE FROM user_memory WHERE user_id = ?", (user_id,))
                else:
                    cur.execute(
                        "DELETE FROM user_memory WHERE user_id = ? AND id = ?",
                        (user_id, fact_id),
                    )

    # ---------- embedding usage ----------

    async def embedding_usage_add(self, day: str, tokens: int) -> None:
        async with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO embedding_usage (day, tokens, calls)
                    VALUES (?, ?, 1)
                    ON CONFLICT(day) DO UPDATE SET
                        tokens = tokens + excluded.tokens,
                        calls = calls + 1
                    """,
                    (day, max(0, int(tokens))),
                )

    async def embedding_usage_get(self, day: str) -> dict[str, int]:
        async with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM embedding_usage WHERE day = ?", (day,)
            ).fetchone()
        if not row:
            return {"tokens": 0, "calls": 0}
        return dict(row)  # type: ignore[return-value]
