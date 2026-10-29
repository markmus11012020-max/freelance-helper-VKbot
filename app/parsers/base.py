from __future__ import annotations

import asyncio
import html
import logging
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

from app.config import Settings
from app.models import ParsedOrder

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


class HttpClient:
    """Small aiohttp wrapper with polite retries."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": self.settings.http_user_agent,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            },
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def text(self, url: str, retries: int = 3) -> str:
        if self._session is None:
            raise FetchError("HTTP client is not started")
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                async with self._session.get(url) as response:
                    if response.status >= 500:
                        raise FetchError(f"HTTP {response.status} for {url}")
                    if response.status >= 400:
                        raise FetchError(f"HTTP {response.status} for {url}")
                    return await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError, FetchError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * attempt)
        raise FetchError(f"failed to fetch {url}: {last_error}")

    async def post_json(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> Any:
        if self._session is None:
            raise FetchError("HTTP client is not started")
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                async with self._session.post(url, data=data, headers=headers) as response:
                    if response.status >= 400:
                        raise FetchError(f"HTTP {response.status} for {url}")
                    return await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, FetchError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * attempt)
        raise FetchError(f"failed to fetch {url}: {last_error}")


class BaseParser(ABC):
    source: str = "base"

    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http

    @abstractmethod
    async def fetch(self) -> list[ParsedOrder]:
        ...

    async def enrich(self, order: ParsedOrder) -> None:
        """Optionally fill missing fields (e.g. budget from a detail page)."""
        return None

    @staticmethod
    def _dedupe(orders: list[ParsedOrder]) -> list[ParsedOrder]:
        seen: set[tuple[str, str]] = set()
        result: list[ParsedOrder] = []
        for order in orders:
            if order.dedup_key in seen:
                continue
            seen.add(order.dedup_key)
            result.append(order)
        return result

    def __repr__(self) -> str:
        return f"<{type(self).__name__} source={self.source}>"


def _clean(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())
