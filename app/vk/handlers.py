from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.db import Database
from app.drafts import DraftService, ensure_greeting
from app.llm import LlmClient
from app.memory import MemoryStore
from app.notifier import Notifier
from app.repo_memory import RepoMemory
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
/draft 123 [Имя] — черновик отклика на заказ
/repos — состояние базы GitHub-проектов
/repos sync — обновить базу (админ)
/repos find запрос — поиск по проектам
/remember текст — запомнить пожелание к откликам
/memory — моя память
/memory clear — очистить историю диалога
/forget 3 — удалить факт; /forget all — все факты
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
        drafts: DraftService | None = None,
        memory: MemoryStore | None = None,
        repos: RepoMemory | None = None,
        github_sync: Any | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.vk = vk
        self.notifier = notifier
        self.llm = llm
        self.scan_now = scan_now
        self.drafts = drafts
        self.memory = memory
        self.repos = repos
        self.github_sync = github_sync

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
            if not is_chat:
                peer = await self.db.get_peer(peer_id)
                if is_admin or (peer is not None and peer.get("status") == "active"):
                    await self._llm_chat(peer_id, from_id, text)
                else:
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
                await self._llm_chat(peer_id, from_id, argument.strip())
        elif command == "/draft":
            await self._cmd_draft(peer_id, from_id, argument)
        elif command == "/repos":
            await self._cmd_repos(peer_id, from_id, is_admin, argument)
        elif command == "/remember":
            await self._cmd_remember(peer_id, from_id, argument)
        elif command == "/memory":
            await self._cmd_memory(peer_id, from_id, argument)
        elif command == "/forget":
            await self._cmd_forget(peer_id, from_id, argument)
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
        repo_line = "GitHub-память: не настроена"
        if self.github_sync is not None:
            repo_stats = await self.github_sync.stats()
            repo_line = (
                f"GitHub-память: {repo_stats.get('indexed', 0)}/"
                f"{repo_stats.get('total', 0)} репозиториев "
                f"(partial {repo_stats.get('partial', 0)}, "
                f"ошибок {repo_stats.get('errors', 0)}), "
                f"{repo_stats.get('chunks', 0)} чанков, "
                f"векторов {repo_stats.get('with_vectors', 0)}\n"
                f"Последняя синхронизация: {repo_stats.get('last_sync') or '—'}"
            )
        emb_usage = await self.db.embedding_usage_get(
            datetime.now(timezone.utc).date().isoformat()
        )
        text = (
            f"Мониторинг: {'⏸ пауза' if paused else '▶️ работает'}\n"
            f"Интервал: {self.settings.scan_interval_seconds} c\n"
            f"Заказов всего: {stats['total']} (сегодня {stats['today']})\n"
            f"Отправлено: {stats['sent']}, в очереди: {stats['pending']}\n"
            f"Последний заказ: {stats['last_order'] or '—'}\n"
            f"LLM: {'on' if self.llm.available else 'off'} · {self.settings.openai_model} · "
            f"токенов сегодня {usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)}\n"
            f"Эмбеддинги сегодня: {emb_usage.get('tokens', 0)} токенов\n"
            f"{repo_line}\n"
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

    async def _cmd_draft(self, peer_id: int, from_id: int, argument: str) -> None:
        parts = argument.strip().split(maxsplit=1)
        if not parts:
            await self._reply(peer_id, "Формат: /draft 123 [Имя заказчика]")
            return
        try:
            order_id = int(parts[0])
        except ValueError:
            await self._reply(peer_id, "Нужен числовой id заказа: /draft 123 [Имя]")
            return
        order = await self.db.get_order(order_id)
        if not order:
            await self._reply(peer_id, "Заказ не найден.")
            return
        await self._send_draft(peer_id, from_id, order, parts[1] if len(parts) > 1 else "")

    async def _send_draft(
        self,
        peer_id: int,
        user_id: int,
        order: dict[str, Any],
        override_name: str = "",
    ) -> None:
        if not self.llm.available:
            await self._reply(peer_id, "ИИ не настроен: добавь OPENAI_API_KEY.")
            return
        order_id = int(order.get("id") or order.get("order_id") or 0)
        if self.memory:
            await self.memory.record(
                peer_id=peer_id,
                user_id=user_id,
                role="user",
                content=f"Сгенерируй черновик отклика на «{order.get('title') or ''}»",
                order_id=order_id or None,
            )

        draft_text = ""
        sources = ""
        if self.drafts is not None:
            result = await self.drafts.generate(
                order,
                user_id=user_id,
                peer_id=peer_id,
                override_name=override_name,
            )
            if result:
                draft_text = result.text
                sources = result.sources_text
        else:
            body = await self.llm.draft_reply(order)
            if body:
                draft_text = ensure_greeting(body, override_name)

        if not draft_text:
            await self._reply(peer_id, "Не получилось сгенерировать отклик, попробуй позже.")
            return

        title = truncate(str(order.get("title") or ""), 60)
        text = f"Черновик отклика на «{title}»:\n\n{draft_text}"
        if sources:
            text += "\n\n— Источники для проверки (не копировать заказчику):\n" + sources
        text = truncate(text, 3900)
        try:
            await self.vk.send_message(user_id, text)
        except VkApiError:
            await self._reply(peer_id, text)

    async def _cmd_repos(
        self, peer_id: int, from_id: int, is_admin: bool, argument: str
    ) -> None:
        if self.github_sync is None:
            await self._reply(peer_id, "GitHub-память не настроена: добавь GITHUB_OWNER.")
            return
        raw = argument.strip()
        if raw.lower() == "sync":
            if not is_admin:
                await self._reply(peer_id, "Обновлять базу может только админ.")
                return
            await self._reply(peer_id, "Запускаю синхронизацию GitHub в фоне…")

            async def run_sync() -> None:
                try:
                    result = await self.github_sync.sync_all()
                except Exception as exc:
                    log.exception("manual GitHub sync failed")
                    await self._reply(
                        peer_id, f"Синхронизация не удалась: {truncate(str(exc), 300)}"
                    )
                    return
                if result.get("busy"):
                    await self._reply(peer_id, "Синхронизация уже идёт.")
                    return
                text = (
                    "GitHub-синхронизация завершена:\n"
                    f"найдено: {result.get('seen', 0)}, "
                    f"обновлено: {result.get('indexed', 0)}, "
                    f"без изменений: {result.get('skipped', 0)}, "
                    f"ошибок: {result.get('errors', 0)}, "
                    f"удалено: {result.get('missing', 0)}"
                )
                if result.get("rate_limited"):
                    text += "\n⚠️ Упёрлись в лимит GitHub. Добавь GITHUB_TOKEN или повтори позже."
                await self._reply(peer_id, text)

            asyncio.create_task(run_sync(), name="manual-github-sync")
            return

        if raw.lower().startswith("find "):
            if self.repos is None:
                await self._reply(peer_id, "Поиск по проектам недоступен.")
                return
            query = raw[5:].strip()
            if not query:
                await self._reply(peer_id, "Формат: /repos find телеграм бот")
                return
            docs = await self.repos.search(query, top_k=5, dedupe_repos=True)
            if not docs:
                await self._reply(peer_id, "Ничего релевантного в базе проектов не нашёл.")
                return
            lines = []
            for doc in docs:
                lines.append(
                    f"{doc.get('score', 0)} · {doc.get('repo_full_name')} "
                    f"({doc.get('kind')})\n{truncate(str(doc.get('content') or ''), 220)}\n"
                    f"{doc.get('url') or ''}"
                )
            await self._reply(peer_id, truncate("\n\n".join(lines), 3900))
            return

        stats = await self.github_sync.stats()
        text = (
            f"GitHub: {stats.get('owner')}\n"
            f"Репозиториев: {stats.get('indexed', 0)}/{stats.get('total', 0)} "
            f"(частично {stats.get('partial', 0)}, ошибок {stats.get('errors', 0)})\n"
            f"Чанков: {stats.get('chunks', 0)}, с векторами: {stats.get('with_vectors', 0)}\n"
            f"Последняя синхронизация: {stats.get('last_sync') or '—'}\n"
            f"Идёт сейчас: {'да' if stats.get('busy') else 'нет'}"
        )
        if stats.get("last_error"):
            text += f"\nПоследняя ошибка: {truncate(str(stats['last_error']), 300)}"
        await self._reply(peer_id, text)

    async def _cmd_remember(self, peer_id: int, from_id: int, argument: str) -> None:
        if self.memory is None:
            await self._reply(peer_id, "Память не настроена.")
            return
        fact = argument.strip()
        if not fact:
            await self._reply(peer_id, "Формат: /remember писать коротко и без цены")
            return
        await self.memory.remember_fact(from_id, fact)
        await self._reply(peer_id, "Запомнил. Это пожелание будет учитываться в откликах.")

    async def _cmd_memory(self, peer_id: int, from_id: int, argument: str) -> None:
        if self.memory is None:
            await self._reply(peer_id, "Память не настроена.")
            return
        if argument.strip().lower() == "clear":
            await self.memory.clear(from_id)
            await self._reply(peer_id, "История диалога очищена. Долгосрочные факты сохранены.")
            return
        facts = await self.memory.facts(from_id)
        recent = await self.memory.context(from_id, limit=8)
        lines = ["Долгосрочные пожелания:"]
        if facts:
            lines.extend(f"{item['id']}. {truncate(str(item['fact']), 300)}" for item in facts)
        else:
            lines.append("— пока нет")
        lines.append("\nПоследние сообщения:")
        lines.append(recent or "— пока нет")
        await self._reply(peer_id, truncate("\n".join(lines), 3900))

    async def _cmd_forget(self, peer_id: int, from_id: int, argument: str) -> None:
        if self.memory is None:
            await self._reply(peer_id, "Память не настроена.")
            return
        raw = argument.strip().lower()
        if raw in {"all", "все", "всё"}:
            await self.memory.forget(from_id, None)
            await self._reply(peer_id, "Все долгосрочные пожелания удалены.")
            return
        if not raw.isdigit():
            await self._reply(peer_id, "Формат: /forget 3 или /forget all")
            return
        await self.memory.forget(from_id, int(raw))
        await self._reply(peer_id, f"Пожелание {raw} удалено.")

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
            order = await self.db.get_order(order_id)
            if not order:
                await self._reply(peer_id, "Заказ не найден.")
                return
            await self._send_draft(peer_id, user_id, order)
            return

        await self._answer_event(event_id, user_id, peer_id, "")

    async def _answer_event(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        try:
            await self.vk.send_event_answer(event_id, user_id, peer_id, text=text)
        except VkApiError as exc:
            log.debug("cannot answer event %s: %s", event_id, exc)

    # ---------- helpers ----------

    async def _llm_chat(self, peer_id: int, user_id: int, text: str) -> None:
        if not self.llm.available:
            await self._reply(peer_id, "ИИ не настроен: добавь OPENAI_API_KEY и OPENAI_BASE_URL.")
            return
        orders = await self.db.recent_orders(limit=5, min_score=self.settings.min_score)
        parts = [
            "Последние заказы:\n"
            + "\n".join(
                f"- {order['title']} | {order['budget_text'] or '—'} | {order['url']}"
                for order in orders
            )
        ]
        if self.memory:
            facts = await self.memory.facts_text(user_id)
            recent = await self.memory.context(user_id, limit=8)
            if facts:
                parts.append("Долгосрочные пожелания пользователя:\n" + facts)
            if recent:
                parts.append("Предыдущие сообщения:\n" + recent)
            await self.memory.record(
                peer_id=peer_id, user_id=user_id, role="user", content=text
            )
        if self.repos:
            try:
                docs = await self.repos.search(text, top_k=3, dedupe_repos=True)
            except Exception:
                log.exception("repo search for chat failed")
                docs = []
            evidence = RepoMemory.evidence_text(docs, limit=500)
            if evidence:
                parts.append("Мои проекты с GitHub:\n" + evidence)
        answer = await self.llm.chat_reply(text, context="\n\n".join(parts))
        if answer and self.memory:
            await self.memory.record(
                peer_id=peer_id, user_id=user_id, role="assistant", content=answer
            )
        await self._reply(peer_id, answer or "Не получилось ответить, попробуй ещё раз.")

    async def _reply(self, peer_id: int, text: str) -> None:
        try:
            await self.vk.send_message(peer_id, truncate(text, 3900))
        except VkApiError as exc:
            log.warning("reply to %s failed: %s", peer_id, exc)
