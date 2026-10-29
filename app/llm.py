from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.db import Database
from app.models import ParsedOrder, ScoreResult
from app.textutil import format_money, truncate

log = logging.getLogger(__name__)

SCORE_SYSTEM = """Ты — опытный фрилансер-разработчик, который ищет заказы под «вайбкодинг»:
быстрая разработка небольших продуктов с помощью ИИ. Тебе хорошо подходят:
телеграм- и VK-боты, парсеры, скрипты, автоматизация, интеграции API, Google Таблицы,
вебхуки, n8n/Make, небольшие веб-приложения на Python.

Плохо подходят (спам для этого профиля): 1С, Bitrix, WordPress, SEO/продвижение,
крипта и смарт-контракты, дизайн, вёрстка, PHP-легаси, копирайтинг, монтаж,
бухгалтерия, вакансии без конкретной задачи.

Оцени заказ по шкале 0-100, где 100 — идеальный жирный заказ под вайбкодинг.
Учитывай: тип задачи, конкретность ТЗ, бюджет (если указан), свежесть, риск.
Верни ТОЛЬКО валидный JSON без markdown:
{"score": 0-100, "fit": true|false, "reason": "1-2 предложения по-русски, почему подходит или нет", "red_flags": ["короткие флаги риска"]}"""

DRAFT_SYSTEM = """Ты — фрилансер-разработчик. Пишешь короткий отклик на заказ.
Стиль: по делу, без воды и без лести, 3-6 предложений. Упомяни релевантный опыт
в 1 предложении, предложи конкретный план из 2-3 шагов и задай 1-2 уточняющих вопроса.
Не занижай цену. Пиши по-русски, обычным текстом без markdown."""

DIGEST_SYSTEM = """Ты — ассистент фрилансера. Сделай короткую сводку заказов за период:
5-8 пунктов, самые жирные сверху, у каждого — заголовок, бюджет и одна строка сути.
В конце — 1 предложение с выводом. Пиши по-русски, обычным текстом, до 1200 символов."""

CHAT_SYSTEM = """Ты — ассистент фрилансера-разработчика в VK-беседе. Отвечай кратко и по делу,
по-русски, обычным текстом. Если вопрос не про заказы, разработку или этот бот — вежливо
верни разговор к делу."""


def _strip_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


class LlmClient:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._client: AsyncOpenAI | None = None
        if settings.llm_configured:
            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                timeout=60.0,
                max_retries=1,
            )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def _budget_left(self) -> bool:
        if not self.available:
            return False
        today = datetime.now(timezone.utc).date().isoformat()
        usage = await self.db.llm_usage_get(today)
        used = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        return used < self.settings.llm_daily_token_limit

    async def _call(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        cache: bool = True,
        max_tokens: int | None = None,
    ) -> str | None:
        if self._client is None or not await self._budget_left():
            return None

        cache_key = hashlib.sha256(
            f"{self.settings.openai_model}|{system}|{user}|{json_mode}".encode()
        ).hexdigest()
        if cache:
            cached = await self.db.llm_cache_get(cache_key)
            if cached:
                return cached

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": self.settings.openai_temperature,
            "max_tokens": max_tokens or self.settings.openai_max_tokens,
        }

        content: str | None = None
        try:
            try:
                response = await self._client.chat.completions.create(
                    **kwargs,
                    response_format={"type": "json_object"} if json_mode else None,
                )
            except Exception as exc:  # provider may not support response_format
                if not json_mode:
                    raise
                log.warning("LLM: response_format rejected (%s), retrying in plain mode", exc)
                response = await self._client.chat.completions.create(**kwargs)

            content = (response.choices[0].message.content or "").strip()
            usage = response.usage
            if usage is not None:
                await self.db.llm_usage_add(
                    datetime.now(timezone.utc).date().isoformat(),
                    int(usage.prompt_tokens or 0),
                    int(usage.completion_tokens or 0),
                )
        except Exception as exc:
            log.warning("LLM call failed: %s", exc)
            return None

        if not content:
            return None
        if cache:
            await self.db.llm_cache_put(cache_key, content)
        return content

    async def score_order(self, order: ParsedOrder, rule: ScoreResult) -> dict[str, Any] | None:
        budget = order.budget_text or (
            format_money(order.budget_min, order.budget_unit) if order.budget_min else "не указан"
        )
        prompt = (
            f"Источник: {order.source}\n"
            f"Категория: {order.category or '—'}\n"
            f"Заголовок: {order.title}\n"
            f"Бюджет: {budget}\n"
            f"Правила дали оценку: {rule.score}/100 (совпало: {', '.join(rule.matched) or '—'})\n"
            f"Описание:\n{truncate(order.description, 1800)}"
        )
        raw = await self._call(SCORE_SYSTEM, prompt, json_mode=True, cache=True, max_tokens=500)
        if not raw:
            return None
        try:
            data = json.loads(_strip_json(raw))
        except json.JSONDecodeError:
            log.warning("LLM: bad JSON for order %s: %s", order.external_id, raw[:200])
            return None
        try:
            data["score"] = max(0, min(100, int(data.get("score", 0))))
        except (TypeError, ValueError):
            return None
        data.setdefault("fit", bool(data["score"] >= self.settings.min_score))
        data.setdefault("reason", "")
        flags = data.get("red_flags") or []
        data["red_flags"] = [str(flag)[:80] for flag in flags if str(flag).strip()][:5]
        return data

    async def draft_reply(self, order: dict[str, Any]) -> str | None:
        prompt = (
            f"Заказ: {order.get('title')}\n"
            f"Источник: {order.get('source')} {order.get('url')}\n"
            f"Категория: {order.get('category') or '—'}\n"
            f"Бюджет: {order.get('budget_text') or 'не указан'}\n"
            f"Описание:\n{truncate(order.get('description', ''), 1800)}"
        )
        return await self._call(DRAFT_SYSTEM, prompt, cache=False, max_tokens=700)

    async def digest(self, orders: list[dict[str, Any]]) -> str | None:
        if not orders:
            return None
        lines = []
        for order in orders[:40]:
            lines.append(
                f"- {order.get('title')} | бюджет: {order.get('budget_text') or '—'} | "
                f"оценка: {order.get('score')} | {order.get('source')}"
            )
        prompt = "Заказы за период:\n" + "\n".join(lines)
        return await self._call(DIGEST_SYSTEM, prompt, cache=True, max_tokens=900)

    async def chat_reply(self, text: str, context: str = "") -> str | None:
        prompt = text if not context else f"Контекст (последние заказы):\n{context}\n\nВопрос: {text}"
        return await self._call(CHAT_SYSTEM, prompt, cache=False, max_tokens=500)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
