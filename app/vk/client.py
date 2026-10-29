from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import zlib
from typing import Any

import aiohttp

from app.config import Settings

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
RETRYABLE_CODES = {6, 9, 10}
MAX_RANDOM_ID = 2**31 - 1


class VkApiError(RuntimeError):
    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"VK {method} failed: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message


class VkClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=45),
        )

    async def close(self) -> None:
        await self._session.close()

    async def call(self, method: str, **params: Any) -> Any:
        if not self.settings.vk_group_token:
            raise VkApiError(method, 0, "VK_GROUP_TOKEN is not configured")

        data = {
            "access_token": self.settings.vk_group_token,
            "v": self.settings.vk_api_version,
            **{key: value for key, value in params.items() if value is not None},
        }
        last_error: VkApiError | None = None

        for attempt in range(1, 4):
            try:
                async with self._session.post(API_URL + method, data=data) as response:
                    if response.status >= 500:
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"HTTP {response.status}",
                        )
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = VkApiError(method, 0, f"network error: {exc}")
                if attempt < 3:
                    await asyncio.sleep(1.0 * attempt)
                    continue
                raise last_error

            error = payload.get("error")
            if not error:
                return payload.get("response")

            code = int(error.get("error_code", 0))
            message = str(error.get("error_msg", "unknown error"))
            last_error = VkApiError(method, code, message)
            if code in RETRYABLE_CODES and attempt < 3:
                delay = 0.5 * attempt if code == 6 else 2.0 * attempt
                log.warning("VK %s: retryable error %s (%s), waiting %.1fs", method, code, message, delay)
                await asyncio.sleep(delay)
                continue
            raise last_error

        raise last_error or VkApiError(method, 0, "unknown error")

    @staticmethod
    def new_random_id() -> int:
        return random.randint(1, MAX_RANDOM_ID)

    async def send_message(
        self,
        peer_id: int,
        message: str,
        *,
        keyboard: dict[str, Any] | None = None,
        attachment: str = "",
        random_id: int | None = None,
        disable_mentions: bool = True,
    ) -> int:
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "message": message[:4000],
            "random_id": random_id if random_id is not None else self.new_random_id(),
            "dont_parse_links": 0,
            "disable_mentions": 1 if disable_mentions else 0,
        }
        if keyboard is not None:
            params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        if attachment:
            params["attachment"] = attachment
        response = await self.call("messages.send", **params)
        # messages.send returns the message id (sometimes a list)
        if isinstance(response, list):
            return int(response[0]) if response else 0
        return int(response or 0)

    async def send_event_answer(
        self,
        event_id: str,
        user_id: int,
        peer_id: int,
        *,
        text: str = "",
        show_snackbar: bool = True,
    ) -> None:
        event_data: dict[str, Any] = {"type": "show_snackbar", "text": text[:90]} if text else {"type": "empty"}
        await self.call(
            "messages.sendMessageEventAnswer",
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data=json.dumps(event_data, ensure_ascii=False),
        )

    async def get_long_poll_server(self) -> dict[str, Any]:
        response = await self.call(
            "groups.getLongPollServer",
            group_id=self.settings.vk_group_id,
        )
        return response

    async def long_poll(
        self,
        server: str,
        key: str,
        ts: str,
        wait: int = 25,
    ) -> dict[str, Any]:
        url = server if server.startswith("http") else f"https://{server}"
        params = {"act": "a_check", "key": key, "ts": ts, "wait": wait, "mode": 2}
        timeout = aiohttp.ClientTimeout(total=wait + 15)
        async with self._session.get(url, params=params, timeout=timeout) as response:
            return await response.json(content_type=None)

    async def add_chat_user(self, chat_id: int, user_id: int) -> None:
        await self.call("messages.addChatUser", chat_id=chat_id, user_id=user_id)

    async def remove_chat_user(self, chat_id: int, user_id: int) -> None:
        await self.call("messages.removeChatUser", chat_id=chat_id, user_id=user_id)

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        response = await self.call("messages.getChat", chat_id=chat_id)
        return response if isinstance(response, dict) else {}

    async def users_info(self, user_ids: list[int]) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        response = await self.call(
            "users.get",
            user_ids=",".join(str(uid) for uid in user_ids),
            fields="screen_name",
        )
        return response if isinstance(response, list) else []

    @staticmethod
    def chat_peer_id(chat_id: int) -> int:
        return 2_000_000_000 + chat_id

    @staticmethod
    def chat_id_from_peer(peer_id: int) -> int:
        return peer_id - 2_000_000_000 if peer_id >= 2_000_000_000 else 0

    @staticmethod
    def is_chat_peer(peer_id: int) -> bool:
        return peer_id >= 2_000_000_000

    @staticmethod
    def random_id_for(order_id: int, peer_id: int) -> int:
        # Stable per (order, peer): a retry of the same delivery is idempotent.
        value = zlib.crc32(f"{order_id}:{peer_id}".encode()) % MAX_RANDOM_ID
        return value or 1

    @staticmethod
    def now_ts() -> str:
        return str(int(time.time()))
