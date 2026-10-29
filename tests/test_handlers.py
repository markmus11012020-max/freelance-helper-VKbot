from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.config import load_settings
from app.db import Database
from app.drafts import DraftResult
from app.llm import LlmClient
from app.models import ParsedOrder
from app.notifier import Notifier
from app.vk.handlers import BotHandlers

ADMIN_ID = 1
USER_ID = 2
CHAT_PEER = 2_000_000_001


class FakeVk:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.added: list[tuple[int, int]] = []

    async def send_message(self, peer_id: int, message: str, **kwargs) -> int:
        self.sent.append({"peer_id": peer_id, "message": message, **kwargs})
        return 1

    async def send_event_answer(self, event_id, user_id, peer_id, **kwargs) -> None:
        return None

    async def add_chat_user(self, chat_id: int, user_id: int) -> None:
        self.added.append((chat_id, user_id))

    async def get_chat(self, chat_id: int) -> dict:
        return {"title": "Test chat"}

    @staticmethod
    def is_chat_peer(peer_id: int) -> bool:
        return peer_id >= 2_000_000_000

    @staticmethod
    def chat_id_from_peer(peer_id: int) -> int:
        return peer_id - 2_000_000_000


def message(peer_id: int, from_id: int, text: str) -> dict:
    return {
        "type": "message_new",
        "object": {"message": {"peer_id": peer_id, "from_id": from_id, "text": text}},
    }


@pytest.fixture
async def env(tmp_path):
    settings = replace(
        load_settings(),
        vk_group_token="test",
        vk_group_id=1,
        admin_user_ids=frozenset({ADMIN_ID}),
    )
    db = Database(str(tmp_path / "handlers.db"))
    await db.init()
    vk = FakeVk()
    notifier = Notifier(settings, db, vk)  # type: ignore[arg-type]
    llm = LlmClient(settings, db)
    handlers = BotHandlers(settings, db, vk, notifier, llm)  # type: ignore[arg-type]
    try:
        yield db, vk, handlers
    finally:
        await llm.close()
        await db.close()


async def test_admin_connects_chat_and_approves_user(env) -> None:
    db, vk, handlers = env

    await handlers.handle_update(message(ADMIN_ID, ADMIN_ID, "/start"))
    admin = await db.get_peer(ADMIN_ID)
    assert admin is not None and admin["role"] == "admin"

    await handlers.handle_update(message(CHAT_PEER, ADMIN_ID, "/connect"))
    chat = await db.get_peer(CHAT_PEER)
    assert chat is not None
    assert chat["kind"] == "chat"
    assert chat["status"] == "active"

    await handlers.handle_update(message(USER_ID, USER_ID, "/start"))
    pending = await db.get_peer(USER_ID)
    assert pending is not None and pending["status"] == "pending"
    assert any("Новая заявка" in item["message"] for item in vk.sent)

    await handlers.handle_update(
        {
            "type": "message_event",
            "object": {
                "event_id": "evt1",
                "user_id": ADMIN_ID,
                "peer_id": ADMIN_ID,
                "event_data": {
                    "payload": json.dumps({"cmd": "approve", "user_id": USER_ID})
                },
            },
        }
    )
    assert vk.added == [(1, USER_ID)]
    approved = await db.get_peer(USER_ID)
    assert approved is not None and approved["status"] == "active"


async def test_non_admin_cannot_add_members(env) -> None:
    db, vk, handlers = env
    await handlers.handle_update(message(USER_ID, USER_ID, "/add 3"))
    assert vk.added == []
    assert any("Только админ" in item["message"] for item in vk.sent)


class FakeDrafts:
    async def generate(self, order, **kwargs):
        return DraftResult(
            text="Анна, Здравствуйте!\n\nСделаю бота на Python.",
            client_name="Анна",
            sources=[
                {
                    "repo_full_name": "owner/bot",
                    "url": "https://github.com/owner/bot",
                }
            ],
        )


class FakeLlmAvailable:
    available = True


async def test_draft_callback_sends_greeting_and_sources(env) -> None:
    db, vk, handlers = env
    order_id, _ = await db.upsert_order(
        ParsedOrder(
            source="fl.ru",
            external_id="draft-1",
            url="https://www.fl.ru/projects/draft-1/x.html",
            title="Нужен Telegram-бот",
            description="Здравствуйте, меня зовут Анна.",
        )
    )
    handlers.llm = FakeLlmAvailable()  # type: ignore[assignment]
    handlers.drafts = FakeDrafts()  # type: ignore[assignment]

    await handlers.handle_update(
        {
            "type": "message_event",
            "object": {
                "event_id": "evt-draft",
                "user_id": USER_ID,
                "peer_id": USER_ID,
                "event_data": {
                    "payload": json.dumps({"cmd": "draft", "order_id": order_id})
                },
            },
        }
    )

    draft_messages = [
        item for item in vk.sent
        if item["peer_id"] == USER_ID and "Черновик отклика" in item["message"]
    ]
    assert draft_messages
    text = draft_messages[-1]["message"]
    assert "Анна, Здравствуйте!" in text
    assert "Источники для проверки" in text
    assert "owner/bot" in text
