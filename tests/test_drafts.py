from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.config import load_settings
from app.db import Database
from app.drafts import (
    DraftService,
    ensure_greeting,
    extract_name_regex,
    normalize_name,
    validate_name,
)
from app.llm import DraftResponse


def test_ensure_greeting_with_name_exists_once() -> None:
    text = ensure_greeting("Иван, здравствуйте! Сделаю бота.", "Иван")
    assert text.startswith("Иван, Здравствуйте!")
    assert text.lower().count("здравствуйте") == 1


def test_ensure_greeting_without_name() -> None:
    text = ensure_greeting("Добрый день! Сделаю бота.", "")
    assert text.startswith("Здравствуйте!")
    assert "Добрый день" not in text


def test_extract_name_from_order_text() -> None:
    assert extract_name_regex("Здравствуйте, меня зовут Анна. Нужен бот.") == "Анна"
    assert extract_name_regex("Здравствуйте, Анна! Нужен бот.") == "Анна"
    assert extract_name_regex("Здравствуйте! Нужен бот для парсинга.") == ""


def test_name_validation_rejects_words_and_unknown_names() -> None:
    assert normalize_name("«анна»") == "Анна"
    assert validate_name("Анна", "Здравствуйте, меня зовут Анна")
    assert validate_name("Анна", "Напишите Анне")
    assert not validate_name("Игнат", "Нужен бот для парсинга")
    assert not validate_name("Заказчик", "Заказчик хочет бота")


class FakeMemory:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    async def facts_text(self, user_id: int) -> str:
        return "- писать коротко"

    async def context(self, user_id: int, limit: int | None = None) -> str:
        return "Пользователь: предыдущий вопрос"


class FakeRepos:
    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "repo_full_name": "owner/project",
                "kind": "profile",
                "content": "Telegram-бот для мониторинга заказов.",
                "url": "https://github.com/owner/project",
                "score": 0.8,
            },
            {
                "repo_full_name": "owner/project",
                "kind": "code",
                "content": "def monitor(): ...",
                "url": "https://github.com/owner/project/blob/main/bot.py",
                "score": 0.7,
            },
        ]


class FakeLlm:
    available = True

    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self.kwargs: dict[str, Any] = {}

    async def draft_reply_with_meta(self, order: dict, **kwargs: Any) -> DraftResponse:
        self.kwargs = kwargs
        return DraftResponse(
            body="Здравствуйте! Сделаю бота на Python.",
            client_name=self.name,
        )

    async def draft_reply(self, order: dict, **kwargs: Any) -> str:
        return "Сделаю бота."


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "drafts.db"))
    await database.init()
    yield database
    await database.close()


async def test_generate_uses_relevant_project_and_greeting(db: Database) -> None:
    settings = replace(load_settings(), rag_final_projects=2)
    memory = FakeMemory()
    llm = FakeLlm()
    service = DraftService(settings, db, llm, FakeRepos(), memory)  # type: ignore[arg-type]
    order = {
        "id": 1,
        "title": "Нужен Telegram-бот",
        "description": "Здравствуйте, меня зовут Анна. Нужен парсер и бот на Python.",
        "source": "fl.ru",
        "url": "https://example.com/1",
        "category": "Разработка",
        "budget_text": "40 000 ₽",
    }

    result = await service.generate(order, user_id=10, peer_id=10)

    assert result is not None
    assert result.client_name == "Анна"
    assert result.text.startswith("Анна, Здравствуйте!")
    assert "owner/project" in result.sources_text
    assert "Telegram-бот для мониторинга" in llm.kwargs["project_context"]
    assert "писать коротко" in llm.kwargs["memory_context"]
    assert memory.records and memory.records[-1]["role"] == "assistant"
    stored = await db.order_client_name_get(1)
    assert stored is not None and stored["client_name"] == "Анна"


async def test_llm_name_is_ignored_if_not_in_order(db: Database) -> None:
    settings = load_settings()
    service = DraftService(settings, db, FakeLlm("Игнат"), FakeRepos(), FakeMemory())  # type: ignore[arg-type]
    order = {
        "id": 2,
        "title": "Нужен парсер",
        "description": "Нужно собрать данные с сайта.",
        "source": "fl.ru",
        "url": "https://example.com/2",
    }

    result = await service.generate(order, user_id=10, peer_id=10)

    assert result is not None
    assert result.client_name == ""
    assert result.text.startswith("Здравствуйте!")
