from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.db import Database
from app.llm import LlmClient
from app.notifier import Notifier
from app.textutil import truncate
from app.vk.client import VkApiError, VkClient

log = logging.getLogger(__name__)

HELP_TEXT = """Команды бота:
/start — подключиться (в личке с сообществом)
/connect — подключить беседу (только админ, отправить в беседе)
/status — состояние мониторинга
/last 5 — последние заказы
/digest — сводка за 24 часа
/ask вопрос — спросить ИИ
/pause — пауза мониторинга (админ)
/resume — продолжить (админ)
/add 12345 — добавить человека в беседу (админ)
/remove 12345 — удалить из беседы (админ)
/whoami — мои id
/help — эта справка"""


class BotHandlers:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        vk: VkClient,
        notifier: Notifier,
        llm: LlmClient,
        scan_now: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.vk = vk
        self.notifier = notifier
        self.llm = llm
        self.scan_now = scan_now

    # ---------- dispatch ----------

    async def handle_update(self, update: dict[str, Any]) -> None:
        event_type = update.get("type")
        if event_type == "message_new":
            await self._on_message(update.get("object", {}).get("message", {}))
        elif event_type == "message_event":
            await self._on_message_event(update.get("object", {}))
        elif event_type == "message_allow":
            user_id = int(update.get("object", {}).get("user_id", 0))
            if user_id:
                log.info("user %s allowed messages", user_id)
                try:
                    await self.vk.send_message(
                        user_id,
                        "Сообщения разрешены. Напиши /start, чтобы получить заявку на добавление в беседу.",
                    )
                except VkApiError:
                    pass

    # ---------- messages ----------

    async def _on_message(self, message: dict[str, Any]) -> None:
        text = (message.get("text") or "").strip()
        peer_id = int(message.get("peer_id") or 0)
        from_id = int(message.get("from_id") or 0)
        if not text or not peer_id or from_id <= 0:
            return

        command, _, argument = text.partition(" ")
        command = command.lower().split("@")[0]
        is_chat = VkClient.is_chat_peer(peer_id)
        is_admin = self.settings.is_admin(from_id)
        reply_user = int(
            (message.get("reply_message") or {}).get("from_id") or 0
        )

        if not command.startswith("/"):
            if not is_chat and is_admin:
                await self._llm_chat(peer_id, text)
            elif not is_chat:
                await self._register_pending(from_id, peer_id)
            return

        if command == "/start":
            await self._cmd_start(peer_id, from_id, is_chat)
        elif command == "/connect":
            await self._cmd_connect(peer_id, from_id, is_admin)
        elif command == "/status":
            await self._cmd_status(peer_id)
        elif command == "/last":
            await self._cmd_last(peer_id, argument)
        elif command == "/digest":
            await self._cmd_digest(peer_id)
        elif command == "/ask":
            if not argument.strip():
                await self._reply(peer_id, "Напиши вопрос после /ask, например: /ask какие заказы сегодня?")
            else:
                await self._llm_chat(peer_id, argument.strip())
        elif command in {"/pause", "/resume"}:
            await self._cmd_pause(peer_id, from_id, is_admin, command == "/pause")
        elif command in {"/add", "/remove"}:
            await self._cmd_chat_member(peer_id, from_id, is_admin, command, argument, reply_user)
        elif command == "/whoami":
            await self._reply(
                peer_id,
                f"user_id: {from_id}\npeer_id: {peer_id}\n"
                f"чат: {VkClient.chat_id_from_peer(peer_id) or '—'}\n"
                f"админ: {'да' if is_admin else 'нет'}",
            )
        elif command in {"/help", "/commands"}:
            await self._reply(peer_id, HELP_TEXT)
        else:
            await self._reply(peer_id, "Неизвестная команда. /help — список команд.")

    # ---------- commands ----------

    async def _cmd_start(self, peer_id: int, from_id: int, is_chat: bool) -> None:
        if is_chat:
            await self._cmd_connect(peer_id, from_id, self.settings.is_admin(from_id))
            return

        if self.settings.is_admin(from_id):
            await self.db.upsert_peer(
                peer_id, kind="user", user_id=from_id, role="admin", status="active", title="admin"
            )
            await self._reply(
                peer_id,
                "Ты админ. Дальше:\n"
                "1. Создай беседу в VK и добавь туда сообщество.\n"
                "2. Отправь в беседе /connect.\n"
                "3. Одобряй заявки кнопкой в личке.\n\n" + HELP_TEXT,
            )
            return

        await self._register_pending(from_id, peer_id)

    async def _register_pending(self, from_id: int, peer_id: int) -> None:
        existing = await self.db.get_peer(peer_id)
        await self.db.upsert_peer(
            peer_id, kind="user", user_id=from_id, role="member", status="pending", title="заявка"
        )
        await self._reply(
            peer_id,
            "Заявка отправлена администратору. Как только тебя добавят в беседу — заказы придут туда.",
        )
        if existing is not None and existing.get("status") == "pending":
            return
        keyboard = {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "callback",
                            "label": "✅ Добавить в беседу",
                            "payload": json.dumps({"cmd": "approve", "user_id": from_id}),
                        }
                    }
                ]
            ],
        }
        await self.notifier.notify_admins(
            f"Новая заявка на заказы.\nuser_id: {from_id}\nДобавить в беседу?",
            keyboard=keyboard,
        )

    async def _cmd_connect(self, peer_id: int, from_id: int, is_admin: bool) -> None:
        if not is_admin:
            await self._reply(peer_id, "Подключить беседу может только админ.")
            return
        if not VkClient.is_chat_peer(peer_id):
            await self._reply(peer_id, "Эту команду нужно отправить в беседе, где есть сообщество.")
            return
        chat_id = VkClient.chat_id_from_peer(peer_id)
        title = ""
        try:
            chat = await self.vk.get_chat(chat_id)
            title = str(chat.get("title") or "")
        except VkApiError as exc:
            log.warning("cannot read chat %s: %s", chat_id, exc)
        await self.db.upsert_peer(
            peer_id, kind="chat", user_id=0, role="chat", status="active", title=title, added_by=from_id
        )
        recent = await self.db.orders_for_backfill(limit=10, min_score=self.settings.min_score)
        for order in recent:
            await self.db.enqueue_delivery(int(order["id"]), peer_id)
        suffix = f"\nОтправляю последних заказов: {len(recent)}." if recent else ""
        await self._reply(
            peer_id,
            f"Беседа подключена: {title or 'без названия'}\nchat_id: {chat_id}\n"
            f"Сюда будут приходить отобранные заказы.{suffix}",
        )
        if recent:
            await self.notifier.deliver_pending()

    async def _cmd_status(self, peer_id: int) -> None:
        stats = await self.db.stats()
        paused = await self.db.kv_get("paused", "0") == "1"
        sources = await self.db.sources()
        usage = await self.db.llm_usage_get(datetime.now(timezone.utc).date().isoformat())
        source_lines = [
            f"• {row['source']}: {'ok' if row['ok'] else 'ошибка'} "
            f"(fail={row['fail_count']}, last_ok={row['last_ok'] or '—'})"
            for row in sources
        ] or ["• ещё не запускался"]
        text = (
            f"Мониторинг: {'⏸ пауза' if paused else '▶️ работает'}\n"
            f"Интервал: {self.settings.scan_interval_seconds} c\n"
            f"Заказов всего: {stats['total']} (сегодня {stats['today']})\n"
            f"Отправлено: {stats['sent']}, в очереди: {stats['pending']}\n"
            f"Последний заказ: {stats['last_order'] or '—'}\n"
            f"LLM: {'on' if self.llm.available else 'off'} · {self.settings.openai_model} · "
            f"токенов сегодня {usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)}\n"
            f"Источники:\n" + "\n".join(source_lines)
        )
        await self._reply(peer_id, text)

    async def _cmd_last(self, peer_id: int, argument: str) -> None:
        try:
            limit = max(1, min(10, int(argument.strip() or "5")))
        except ValueError:
            limit = 5
        orders = await self.db.recent_orders(limit=limit)
        if not orders:
            await self._reply(peer_id, "Пока пусто — заказов ещё не было.")
            return
        lines = []
        for order in orders:
            lines.append(
                f"{order['score']}/100 · {order['title']}\n"
                f"💰 {order['budget_text'] or 'не указан'}\n{order['url']}"
            )
        await self._reply(peer_id, truncate("\n\n".join(lines), 3900))

    async def _cmd_digest(self, peer_id: int) -> None:
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        orders = await self.db.orders_since(since, min_score=max(0, self.settings.min_score - 25))
        if not orders:
            await self._reply(peer_id, "За сутки ничего не нашлось.")
            return
        summary = await self.llm.digest(orders)
        if not summary:
            lines = [
                f"{order['score']}/100 · {order['title']} · {order['budget_text'] or '—'}"
                for order in orders[:10]
            ]
            summary = "Сводка за 24 часа:\n" + "\n".join(lines)
        await self._reply(peer_id, summary)

    async def _cmd_pause(self, peer_id: int, from_id: int, is_admin: bool, pause: bool) -> None:
        if not is_admin:
            await self._reply(peer_id, "Только админ может ставить паузу.")
            return
        await self.db.kv_set("paused", "1" if pause else "0")
        await self._reply(peer_id, "Мониторинг на паузе." if pause else "Мониторинг продолжает работу.")
        if self.scan_now and not pause:
            try:
                await self.scan_now()
            except Exception:
                log.exception("manual scan failed")

    async def _cmd_chat_member(
        self,
        peer_id: int,
        from_id: int,
        is_admin: bool,
        command: str,
        argument: str,
        reply_user: int,
    ) -> None:
        if not is_admin:
            await self._reply(peer_id, "Только админ может управлять участниками.")
            return
        raw = argument.strip().lstrip("@")
        if raw.startswith("id"):
            raw = raw[2:]
        user_id = int(raw) if raw.isdigit() else reply_user
        if not user_id:
            await self._reply(
                peer_id,
                "Укажи user_id: /add 12345 — или ответь командой на сообщение человека.",
            )
            return

        chat_peer = await self._pick_chat_peer(peer_id if VkClient.is_chat_peer(peer_id) else 0)
        if not chat_peer:
            await self._reply(peer_id, "Нет подключённой беседы. Сначала /connect в беседе.")
            return
        chat_id = VkClient.chat_id_from_peer(chat_peer)
        try:
            if command == "/add":
                await self.vk.add_chat_user(chat_id, user_id)
                await self.db.upsert_peer(
                    user_id, kind="user", user_id=user_id, role="member", status="active", title="member"
                )
                await self._reply(peer_id, f"Пользователь {user_id} добавлен в беседу.")
            else:
                await self.vk.remove_chat_user(chat_id, user_id)
                await self._reply(peer_id, f"Пользователь {user_id} удалён из беседы.")
        except VkApiError as exc:
            await self._reply(
                peer_id,
                f"VK не дал изменить состав беседы: {exc.message}.\n"
                "Добавь вручную в VK — бот продолжит писать в эту беседу.",
            )

    async def _pick_chat_peer(self, preferred: int) -> int:
        if preferred:
            peer = await self.db.get_peer(preferred)
            if peer and peer["status"] == "active":
                return preferred
        chats = [
            peer
            for peer in await self.db.list_peers(status="active")
            if peer["kind"] == "chat"
        ]
        return int(chats[0]["peer_id"]) if chats else 0

    # ---------- callbacks ----------

    async def _on_message_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("event_id") or "")
        user_id = int(event.get("user_id") or 0)
        peer_id = int(event.get("peer_id") or 0)
        raw_payload = (event.get("event_data") or {}).get("payload") or "{}"
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            payload = {}
        command = payload.get("cmd")
        is_admin = self.settings.is_admin(user_id)

        if command == "approve":
            target = int(payload.get("user_id") or 0)
            if not is_admin:
                await self._answer_event(event_id, user_id, peer_id, "Только админ может одобрять")
                return
            chat_peer = await self._pick_chat_peer(peer_id if VkClient.is_chat_peer(peer_id) else 0)
            if not chat_peer:
                await self._answer_event(event_id, user_id, peer_id, "Сначала /connect в беседе")
                return
            chat_id = VkClient.chat_id_from_peer(chat_peer)
            try:
                await self.vk.add_chat_user(chat_id, target)
            except VkApiError as exc:
                log.warning("approve %s failed: %s", target, exc)
                await self._answer_event(event_id, user_id, peer_id, "VK не дал добавить")
                await self._reply(
                    peer_id,
                    f"Не получилось добавить {target}: {exc.message}.\n"
                    "Добавь вручную в VK, бот продолжит писать в беседу.",
                )
            else:
                await self.db.upsert_peer(
                    target, kind="user", user_id=target, role="member", status="active", title="member"
                )
                await self._answer_event(event_id, user_id, peer_id, "Добавлен")
                await self._reply(peer_id, f"Пользователь {target} добавлен в беседу.")
                try:
                    await self.vk.send_message(
                        target,
                        "Тебя добавили в беседу с заказами. Заказы будут приходить туда.",
                    )
                except VkApiError:
                    pass
            return

        if command == "draft":
            order_id = int(payload.get("order_id") or 0)
            await self._answer_event(event_id, user_id, peer_id, "Генерирую отклик…")
            if not self.llm.available:
                await self._reply(peer_id, "ИИ не настроен: добавь OPENAI_API_KEY.")
                return
            order = await self.db.get_order(order_id)
            if not order:
                await self._reply(peer_id, "Заказ не найден.")
                return
            draft = await self.llm.draft_reply(order)
            if not draft:
                await self._reply(peer_id, "Не получилось сгенерировать отклик, попробуй позже.")
                return
            text = f"Черновик отклика на «{truncate(order['title'], 60)}»:\n\n{draft}"
            try:
                await self.vk.send_message(user_id, text)
            except VkApiError:
                await self._reply(peer_id, text)
            return

        await self._answer_event(event_id, user_id, peer_id, "")

    async def _answer_event(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        try:
            await self.vk.send_event_answer(event_id, user_id, peer_id, text=text)
        except VkApiError as exc:
            log.debug("cannot answer event %s: %s", event_id, exc)

    # ---------- helpers ----------

    async def _llm_chat(self, peer_id: int, text: str) -> None:
        if not self.llm.available:
            await self._reply(peer_id, "ИИ не настроен: добавь OPENAI_API_KEY и OPENAI_BASE_URL.")
            return
        orders = await self.db.recent_orders(limit=5, min_score=self.settings.min_score)
        context = "\n".join(
            f"- {order['title']} | {order['budget_text'] or '—'} | {order['url']}" for order in orders
        )
        answer = await self.llm.chat_reply(text, context=context)
        await self._reply(peer_id, answer or "Не получилось ответить, попробуй ещё раз.")

    async def _reply(self, peer_id: int, text: str) -> None:
        try:
            await self.vk.send_message(peer_id, truncate(text, 3900))
        except VkApiError as exc:
            log.warning("reply to %s failed: %s", peer_id, exc)
