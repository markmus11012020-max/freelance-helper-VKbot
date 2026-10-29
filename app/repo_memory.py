from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from app.config import Settings
from app.db import Database
from app.embeddings import EmbeddingClient, EmbeddingError
from app.textutil import stem_text, truncate

log = logging.getLogger(__name__)


class RepoMemory:
    """Persistent repository knowledge with vector + lexical retrieval.

    Vectors live in SQLite (`repo_documents.embedding`).  A compact numpy
    matrix is kept in memory for fast cosine search; it is rebuilt lazily
    after a GitHub sync.
    """

    def __init__(self, settings: Settings, db: Database, embeddings: EmbeddingClient) -> None:
        self.settings = settings
        self.db = db
        self.embeddings = embeddings
        self._meta: list[dict[str, Any]] = []
        self._tokens: list[set[str]] = []
        self._matrix: np.ndarray | None = None
        self._vector_meta_indices: list[int] = []
        self._dirty = True
        self._lock = asyncio.Lock()

    async def load(self, force: bool = False) -> None:
        async with self._lock:
            if not self._dirty and not force:
                return
            rows = await self.db.repo_documents_all()
            self._meta = []
            self._tokens = []
            vectors: list[np.ndarray] = []
            vector_meta_indices: list[int] = []
            expected_dim = self.settings.embedding_dimensions

            for row in rows:
                meta = {key: row[key] for key in row if key != "embedding"}
                meta["content"] = str(row.get("content") or "")
                self._meta.append(meta)
                self._tokens.append(set(stem_text(meta["content"]).split()))

                vector = self.embeddings.decode(row.get("embedding"), expected_dim)
                if vector is not None and len(vector) > 0:
                    vectors.append(vector)
                    vector_meta_indices.append(len(self._meta) - 1)

            if vectors:
                try:
                    self._matrix = np.vstack(vectors).astype(np.float32)
                    self._vector_meta_indices = vector_meta_indices
                except ValueError:
                    log.exception("repo memory has vectors with inconsistent dimensions")
                    self._matrix = None
                    self._vector_meta_indices = []
            else:
                self._matrix = None
                self._vector_meta_indices = []
            self._dirty = False
            log.info(
                "repo memory loaded: %s documents, %s vectors",
                len(self._meta),
                len(self._vector_meta_indices),
            )

    def mark_dirty(self) -> None:
        self._dirty = True

    async def stats(self) -> dict[str, Any]:
        return await self.db.repo_stats()

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        dedupe_repos: bool = False,
    ) -> list[dict[str, Any]]:
        await self.load()
        if not self._meta:
            return []

        top_k = max(1, top_k or self.settings.rag_top_k)
        min_score = self.settings.rag_min_score if min_score is None else min_score
        query_tokens = set(stem_text(query).split())
        vector_scores = await self._vector_scores(query)

        scored: list[tuple[float, int]] = []
        for index, meta in enumerate(self._meta):
            lexical = 0.0
            if query_tokens and self._tokens[index]:
                lexical = len(query_tokens & self._tokens[index]) / len(query_tokens)
            vector = vector_scores[index] if vector_scores is not None else 0.0
            score = vector * 0.72 + lexical * 0.28
            if meta.get("kind") == "profile":
                score += 0.05
            elif meta.get("kind") == "readme":
                score += 0.02
            if score >= min_score:
                scored.append((score, index))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not dedupe_repos:
            selected: list[dict[str, Any]] = []
            for score, index in scored[:top_k]:
                item = dict(self._meta[index])
                item["score"] = round(float(score), 4)
                selected.append(item)
            return selected

        # One document per repository, preferring the generated project card
        # over an individual code chunk even if the chunk scored slightly higher.
        best_by_repo: dict[str, dict[str, Any]] = {}
        for score, index in scored:
            meta = self._meta[index]
            repo = str(meta.get("repo_full_name") or "")
            if not repo:
                continue
            current = best_by_repo.get(repo)
            if current is None:
                item = dict(meta)
                item["score"] = round(float(score), 4)
                best_by_repo[repo] = item
                continue
            if meta.get("kind") == "profile" and current.get("kind") != "profile":
                item = dict(meta)
                item["score"] = round(float(score), 4)
                best_by_repo[repo] = item
            elif (
                meta.get("kind") != "profile"
                and current.get("kind") != "profile"
                and float(score) > float(current.get("score") or 0)
            ):
                item = dict(meta)
                item["score"] = round(float(score), 4)
                best_by_repo[repo] = item
        selected = sorted(
            best_by_repo.values(), key=lambda item: item.get("score", 0), reverse=True
        )[:top_k]
        return selected

    async def _vector_scores(self, query: str) -> np.ndarray | None:
        if self._matrix is None or not self.embeddings.available:
            return None
        try:
            query_vector = np.asarray(await self.embeddings.embed_one(query), dtype=np.float32)
        except EmbeddingError as exc:
            log.warning("query embedding failed, lexical fallback: %s", exc)
            return None
        except Exception:
            log.exception("query embedding crashed, lexical fallback")
            return None
        if len(query_vector) != self._matrix.shape[1]:
            log.warning(
                "query embedding dimension mismatch: query=%s index=%s",
                len(query_vector),
                self._matrix.shape[1],
            )
            return None
        norm = float(np.linalg.norm(query_vector))
        if norm <= 0:
            return None
        query_vector = query_vector / norm
        scores = self._matrix @ query_vector
        result = np.zeros(len(self._meta), dtype=np.float32)
        for score, index in zip(scores.tolist(), self._vector_meta_indices):
            result[index] = float(score)
        return result

    @staticmethod
    def evidence_text(docs: list[dict[str, Any]], limit: int = 500) -> str:
        """Format retrieved docs as compact facts for the draft prompt."""
        if not docs:
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            repo = str(doc.get("repo_full_name") or "?")
            if repo in seen:
                continue
            seen.add(repo)
            summary = truncate(str(doc.get("content") or ""), limit)
            lines.append(
                f"- {repo} ({doc.get('kind') or 'code'}): {summary}\n"
                f"  Ссылка: {doc.get('url') or '—'}"
            )
        return "\n".join(lines)
