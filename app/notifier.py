from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import Database
from app.textutil import format_money, truncate
from app.vk.client import VkApiError, VkClient

log = logging.getLogger(__name__)


def _score_emoji(score: int) -> str:
    if score >= 80:
        return "🔥"
    if score >= 65:
        return "✅"
    return "👀"


def _published_label(value: str | None, tz: str) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ZoneInfo(tz))
    now = datetime.now(ZoneInfo(tz))
    if local.date() == now.date():
        return local.strftime("сегодня %H:%M")
    return local.strftime("%d.%m %H:%M")


class Notifier:
    def __init__(self, settings: Settings, db: Database, vk: VkClient) -> None:
        self.settings = settings
        self.db = db
        self.vk = vk

    def build_card(self, row: dict[str, Any]) -> str:
        score = int(row.get("score") or 0)
        source = row.get("source") or "?"
        budget = row.get("budget_text") or (
            format_money(row.get("budget_min"), row.get("budget_unit") or "project")
            if row.get("budget_min")
            else "не указан"
        )
        reason = row.get("llm_reason") or ""
        if not reason:
            try:
                matched = json.loads(row.get("matched_keywords") or "[]")
            except json.JSONDecodeError:
                matched = []
            reason = "Совпало: " + ", ".join(matched[:5]) if matched else ""

        lines = [
            f"{_score_emoji(score)} {score}/100 · {source}",
            f"📌 {row.get('title') or '—'}",
            f"💰 {budget}",
        ]
        category = row.get("category") or ""
        if category:
            lines.append(f"🏷 {truncate(category, 90)}")
        lines.append(f"🕐 {_published_label(row.get('published_at'), self.settings.tz)}")
        description = truncate(row.get("description") or "", 600)
        if description:
            lines.extend(["", description])
        if reason:
            lines.extend(["", f"✅ {truncate(reason, 300)}"])
        url = row.get("url") or ""
        if url:
            lines.extend(["", f"🔗 {url}"])
        return truncate("\n".join(lines), 3900, "…")

    def _link_for(self, row: dict[str, Any]) -> str:
        url = str(row.get("url") or "")
        order_id = row.get("order_id") or row.get("id")
        if self.settings.public_base_url and order_id:
            return f"{self.settings.public_base_url}/go/{int(order_id)}"
        return url

    def order_keyboard(self, row: dict[str, Any], *, include_link: bool = True) -> dict[str, Any] | None:
        url = self._link_for(row)
        order_id = row.get("order_id") or row.get("id")
        buttons: list[dict[str, Any]] = []
        if order_id:
            buttons.append(
                {
                    "action": {
                        "type": "callback",
                        "label": "✍️ Черновик",
                        "payload": json.dumps({"cmd": "draft", "order_id": int(order_id)}),
                    }
                }
            )
        if url and include_link:
            buttons.append(
                {
                    "action": {
                        "type": "open_link",
                        "label": "🚀 Откликнуться",
                        "link": url,
                    }
                }
            )
        if not buttons:
            return None
        return {"inline": True, "buttons": [buttons]}

    async def send_order(self, row: dict[str, Any], peer_id: int) -> None:
        text = self.build_card(row)
        order_id = int(row.get("order_id") or row.get("id") or 0)
        delivery_id = int(row.get("delivery_id") or 0)
        random_id = VkClient.random_id_for(order_id, peer_id, delivery_id)
        try:
            await self.vk.send_message(
                peer_id,
                text,
                keyboard=self.order_keyboard(row),
                random_id=random_id,
            )
        except VkApiError as exc:
            # VK rejects open_link buttons for some domains (e.g. kwork.ru)
            # with error 911. The URL is already in the card text, so retry
            # with the callback button only.
            if exc.code == 911 and "link" in exc.message.lower():
                log.info(
                    "VK rejected link button for order %s (%s), retrying without link",
                    order_id,
                    exc.message,
                )
                await self.vk.send_message(
                    peer_id,
                    text,
                    keyboard=self.order_keyboard(row, include_link=False),
                    random_id=random_id,
                )
                return
            raise

    async def deliver_pending(self, limit: int = 30) -> int:
        if self.settings.dry_run or not self.settings.vk_configured:
            return 0
        deliveries = await self.db.pending_deliveries(limit=limit)
        sent = 0
        for delivery in deliveries:
            peer_id = int(delivery["peer_id"])
            order_id = int(delivery["order_id"])
            row = {**delivery, "id": order_id, "delivery_id": int(delivery["id"])}
            try:
                await self.send_order(row, peer_id)
            except VkApiError as exc:
                log.warning(
                    "delivery %s for peer %s failed: %s",
                    order_id,
                    peer_id,
                    exc,
                )
                await self.db.mark_delivery(int(delivery["id"]), "failed", str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("delivery %s crashed", order_id)
                await self.db.mark_delivery(int(delivery["id"]), "failed", str(exc))
            else:
                await self.db.mark_delivery(int(delivery["id"]), "sent")
                sent += 1
            await asyncio.sleep(0.34)  # stay far below VK flood limits
        return sent

    async def notify_admins(self, text: str, keyboard: dict[str, Any] | None = None) -> None:
        if self.settings.dry_run or not self.settings.vk_configured:
            log.info("DRY-RUN admin notification: %s", truncate(text, 500))
            return
        for user_id in self.settings.admin_user_ids:
            try:
                await self.vk.send_message(user_id, truncate(text, 3900), keyboard=keyboard)
            except VkApiError as exc:
                log.warning("cannot notify admin %s: %s", user_id, exc)
            await asyncio.sleep(0.2)
