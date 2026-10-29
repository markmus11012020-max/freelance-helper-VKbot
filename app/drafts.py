from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.db import Database
from app.llm import LlmClient
from app.memory import MemoryStore
from app.repo_memory import RepoMemory
from app.textutil import truncate

log = logging.getLogger(__name__)

_NAME_TOKEN_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{1,30}$")
_NAME_PATTERNS = [
    re.compile(
        r"(?:меня\s+зовут|мо[её]\s+имя|зовут\s+меня|контактное\s+лицо|"
        r"обращайтесь|можно\s+обращаться)\s+(?:ко\s+мне\s+)?"
        r"([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{1,30})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:здравствуйте|добрый\s+день|добрый\s+вечер|привет)\s*[,!]?\s*"
        r"([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{1,30})",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{1,30})\s*[,!]\s*"
        r"(?:здравствуйте|добрый\s+день|привет)",
        re.IGNORECASE,
    ),
]
_GREETING_PREFIX_RE = re.compile(
    r"^\s*(?:(?:[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{1,30})\s*,\s*)?"
    r"(?:здравствуйте|здравствуй|добрый\s+день|добрый\s+вечер|привет)"
    r"(?:\s*,\s*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{1,30})?"
    r"[!.,]?\s*",
    re.IGNORECASE,
)
_NAME_BLACKLIST = {
    "здравствуйте", "здравствуй", "добрый", "день", "вечер", "привет",
    "заказчик", "заказчица", "клиент", "компания", "проект", "меня",
    "ищу", "нужен", "нужна", "требуется", "исполнитель", "фрилансер",
    "разработчик", "специалист", "команда", "сервис", "бот", "сайт",
    "менеджер", "руководитель", "директор", "владелец", "основатель",
    "по", "от", "до", "для", "или", "мы", "вы", "он", "она", "это",
}


@dataclass(slots=True)
class DraftResult:
    text: str
    client_name: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sources_text(self) -> str:
        if not self.sources:
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for source in self.sources:
            repo = str(source.get("repo_full_name") or "")
            if not repo or repo in seen:
                continue
            seen.add(repo)
            lines.append(f"- {repo}: {source.get('url') or 'ссылка не сохранена'}")
        return "\n".join(lines)


def normalize_name(raw: str | None) -> str:
    name = " ".join((raw or "").strip().strip("\"'«».,!").split())
    if not name:
        return ""
    name = name.split()[0]
    if not _NAME_TOKEN_RE.match(name):
        return ""
    if name.lower() in _NAME_BLACKLIST:
        return ""
    if name.isupper() or name.islower():
        name = name.title()
    return name[:1].upper() + name[1:]


def validate_name(name: str, source_text: str) -> bool:
    clean = normalize_name(name)
    if not clean:
        return False
    source = (source_text or "").lower().replace("ё", "е")
    needle = clean.lower().replace("ё", "е")
    # Russian names are often inflected ("Анне", "Ивану"). A short stem is
    # enough here; the LLM is additionally told not to guess.
    probe = needle if len(needle) <= 3 else needle[:3]
    return probe in source


def extract_name_regex(text: str) -> str:
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        candidate = normalize_name(match.group(1))
        if candidate and validate_name(candidate, text):
            return candidate
    return ""


def strip_greeting(text: str) -> str:
    body = (text or "").strip()
    for _ in range(2):
        updated = _GREETING_PREFIX_RE.sub("", body, count=1).strip()
        if updated == body:
            break
        body = updated
    return body


def ensure_greeting(text: str, name: str = "") -> str:
    prefix = f"{normalize_name(name)}, Здравствуйте!" if normalize_name(name) else "Здравствуйте!"
    body = strip_greeting(text)
    return f"{prefix}\n\n{body}" if body else prefix


class DraftService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: LlmClient,
        repos: RepoMemory,
        memory: MemoryStore,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.repos = repos
        self.memory = memory

    async def generate(
        self,
        order: dict[str, Any],
        *,
        user_id: int,
        peer_id: int,
        override_name: str = "",
    ) -> DraftResult | None:
        order_id = int(order.get("id") or order.get("order_id") or 0)
        client_name = await self._resolve_name(order, order_id, override_name)
        query = self._query(order)
        try:
            found = await self.repos.search(
                query, top_k=self.settings.rag_top_k, dedupe_repos=True
            )
        except Exception:
            log.exception("repository search failed")
            found = []
        sources = self._select_sources(found, self.settings.rag_final_projects)
        project_context = self._format_projects(sources)
        memory_context = await self._memory_context(user_id)

        response = await self.llm.draft_reply_with_meta(
            order,
            client_name=client_name,
            project_context=project_context,
            memory_context=memory_context,
        )
        if response is None:
            plain = await self.llm.draft_reply(
                order,
                client_name=client_name,
                project_context=project_context,
                memory_context=memory_context,
            )
            if not plain:
                return None
            body = plain
        else:
            body = response.body
            if not client_name and response.client_name:
                candidate = normalize_name(response.client_name)
                source_text = f"{order.get('title', '')}\n{order.get('description', '')}"
                if candidate and validate_name(candidate, source_text):
                    client_name = candidate
                    if order_id > 0:
                        await self.db.order_client_name_set(order_id, candidate, "llm")

        text = ensure_greeting(body, client_name)
        await self.memory.record(
            peer_id=peer_id,
            user_id=user_id,
            role="assistant",
            content=text,
            order_id=order_id or None,
        )
        return DraftResult(text=text, client_name=client_name, sources=sources)

    async def _resolve_name(
        self, order: dict[str, Any], order_id: int, override_name: str
    ) -> str:
        manual = normalize_name(override_name)
        if manual:
            if order_id > 0:
                await self.db.order_client_name_set(order_id, manual, "manual")
            return manual
        if order_id > 0:
            existing = await self.db.order_client_name_get(order_id)
            if existing and existing.get("client_name"):
                return normalize_name(str(existing["client_name"]))
        source_text = f"{order.get('title', '')}\n{order.get('description', '')}"
        found = extract_name_regex(source_text)
        if found:
            if order_id > 0:
                await self.db.order_client_name_set(order_id, found, "regex")
            return found
        return ""

    @staticmethod
    def _query(order: dict[str, Any]) -> str:
        return truncate(
            f"{order.get('title') or ''}\n{order.get('description') or ''}\n"
            f"{order.get('category') or ''}",
            3000,
        )

    @staticmethod
    def _select_sources(docs: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        best_by_repo: dict[str, dict[str, Any]] = {}
        for doc in docs:
            repo = str(doc.get("repo_full_name") or "")
            if not repo:
                continue
            current = best_by_repo.get(repo)
            if current is None:
                best_by_repo[repo] = doc
                continue
            if doc.get("kind") == "profile" and current.get("kind") != "profile":
                best_by_repo[repo] = doc
            elif float(doc.get("score") or 0) > float(current.get("score") or 0):
                best_by_repo[repo] = doc
        ordered = sorted(
            best_by_repo.values(), key=lambda item: float(item.get("score") or 0), reverse=True
        )
        return ordered[: max(1, limit)]

    @staticmethod
    def _format_projects(docs: list[dict[str, Any]]) -> str:
        if not docs:
            return ""
        parts: list[str] = []
        for index, doc in enumerate(docs, start=1):
            parts.append(
                f"{index}. Проект: {doc.get('repo_full_name')}\n"
                f"Что известно: {truncate(str(doc.get('content') or ''), 900)}\n"
                f"Ссылка: {doc.get('url') or '—'}"
            )
        return "\n\n".join(parts)

    async def _memory_context(self, user_id: int) -> str:
        facts = await self.memory.facts_text(user_id)
        recent = await self.memory.context(user_id, limit=8)
        parts = []
        if facts:
            parts.append("Долгосрочные пожелания:\n" + facts)
        if recent:
            parts.append("Последние сообщения:\n" + recent)
        return "\n\n".join(parts)
