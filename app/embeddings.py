from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
from openai import AsyncOpenAI

from app.config import Settings
from app.db import Database

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


class EmbeddingBudgetExceeded(EmbeddingError):
    pass


class EmbeddingClient:
    """OpenAI-compatible embeddings with batching, retries and budget tracking."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._client: AsyncOpenAI | None = None
        self._send_dimensions = settings.embedding_dimensions > 0
        if settings.embeddings_configured:
            self._client = AsyncOpenAI(
                api_key=settings.embedding_api_key or settings.openai_api_key,
                base_url=settings.embedding_base_url or settings.openai_base_url,
                timeout=float(settings.embedding_timeout_seconds),
                max_retries=0,
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def spec(self) -> str:
        return f"{self.settings.embedding_model}:{self.settings.embedding_dimensions}"

    async def _budget_left(self) -> bool:
        if not self.available:
            return False
        day = datetime.now(timezone.utc).date().isoformat()
        usage = await self.db.embedding_usage_get(day)
        return int(usage.get("tokens", 0)) < self.settings.embedding_daily_token_limit

    async def _create(self, batch: list[str]) -> Any:
        assert self._client is not None
        kwargs: dict[str, Any] = {
            "model": self.settings.embedding_model,
            "input": batch,
        }
        if self._send_dimensions and self.settings.embedding_dimensions > 0:
            kwargs["dimensions"] = self.settings.embedding_dimensions

        for attempt in range(1, 4):
            try:
                return await self._client.embeddings.create(**kwargs)
            except Exception as exc:
                status = int(getattr(exc, "status_code", 0) or 0)
                message = str(exc).lower()
                if self._send_dimensions and status in {400, 422} and "dimension" in message:
                    log.warning("embedding provider rejected dimensions, retrying without it")
                    self._send_dimensions = False
                    kwargs.pop("dimensions", None)
                    continue
                if attempt >= 3 or status in {400, 401, 403, 404}:
                    raise EmbeddingError(f"embedding request failed: {exc}") from exc
                delay = 2.0 * attempt
                log.warning("embedding request failed (%s), retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
        raise EmbeddingError("embedding request failed after retries")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return L2-normalized vectors for the given texts."""
        if not self.available:
            raise EmbeddingError("embeddings are not configured")
        if not texts:
            return []

        cleaned = [" ".join((text or "").split())[:12_000] or " " for text in texts]
        vectors: list[list[float]] = []
        batch_size = max(1, self.settings.embedding_batch_size)

        for start in range(0, len(cleaned), batch_size):
            if not await self._budget_left():
                raise EmbeddingBudgetExceeded("daily embedding token limit reached")
            batch = cleaned[start:start + batch_size]
            response = await self._create(batch)
            data = sorted(
                response.data,
                key=lambda item: int(getattr(item, "index", 0) or 0),
            )
            for item in data:
                vector = np.asarray(item.embedding, dtype=np.float32)
                norm = float(np.linalg.norm(vector))
                if norm > 0:
                    vector = vector / norm
                vectors.append(vector.astype(np.float32).tolist())

            usage = getattr(response, "usage", None)
            tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
            if not tokens:
                tokens = sum(max(1, len(text) // 4) for text in batch)
            await self.db.embedding_usage_add(
                datetime.now(timezone.utc).date().isoformat(), tokens
            )

        if len(vectors) != len(cleaned):
            raise EmbeddingError(
                f"embedding count mismatch: expected {len(cleaned)}, got {len(vectors)}"
            )
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]

    def validate_dimension(self, vector: list[float]) -> bool:
        expected = self.settings.embedding_dimensions
        return expected <= 0 or len(vector) == expected

    @staticmethod
    def encode(vector: list[float] | np.ndarray) -> bytes:
        return np.asarray(vector, dtype=np.float32).tobytes()

    @staticmethod
    def decode(blob: bytes | memoryview | None, dimension: int) -> np.ndarray | None:
        if not blob:
            return None
        try:
            vector = np.frombuffer(bytes(blob), dtype=np.float32)
        except (TypeError, ValueError):
            return None
        if dimension > 0 and len(vector) != dimension:
            return None
        return vector

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
