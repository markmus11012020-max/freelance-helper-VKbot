from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Iterable

from app.config import Settings
from app.db import Database
from app.textutil import normalize

# Canonical competence -> aliases that may appear in a vacancy.
# Keep aliases specific enough: bare "bot", "api" and "go" create too much noise.
SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "python": ("python", "питон"),
    "telegram bot": (
        "telegram bot", "telegram-бот", "телеграм бот", "тг бот", "aiogram", "pyrogram",
    ),
    "vk bot": ("vk bot", "вк бот", "vkbottle", "vk api", "vk long poll"),
    "discord bot": ("discord bot", "discord"),
    "parser": ("парсер", "парсинг", "scraper", "scraping", "beautifulsoup", "bs4", "lxml"),
    "automation": ("автоматизация", "automation", "n8n", "make.com", "zapier"),
    "api integration": (
        "rest api", "api integration", "интеграция api", "webhook", "webhooks", "graphql",
    ),
    "llm": (
        "llm", "large language model", "gpt", "openai", "langchain", "langgraph",
        "rag", "embedding", "embeddings", "ollama", "gigachat",
    ),
    "ai agent": ("ai agent", "ии агент", "autogen", "crewai", "mcp"),
    "fastapi": ("fastapi",),
    "flask": ("flask",),
    "django": ("django",),
    "streamlit": ("streamlit",),
    "gradio": ("gradio",),
    "sql": ("sql", "postgres", "postgresql", "mysql", "sqlite", "sqlalchemy"),
    "vector db": ("pinecone", "qdrant", "chroma", "chromadb", "faiss", "weaviate", "milvus"),
    "docker": ("docker", "docker-compose", "kubernetes", "k8s"),
    "typescript": ("typescript",),
    "javascript": ("javascript",),
    "react": ("react", "reactjs", "next.js", "nextjs"),
    "vue": ("vue", "nuxt"),
    "svelte": ("svelte",),
    "html": ("html", "html5"),
    "css": ("css", "scss", "tailwind"),
    "java": ("java", "spring boot", "spring"),
    "kotlin": ("kotlin",),
    "csharp": ("c#", ".net", "dotnet"),
    "cpp": ("c++", "cpp"),
    "c": ("c language",),
    "go": ("golang",),
    "rust": ("rust",),
    "php": ("php",),
    "laravel": ("laravel",),
    "wordpress": ("wordpress", "woocommerce"),
    "ruby": ("ruby", "rails"),
    "swift": ("swift",),
    "crypto": ("crypto", "web3", "solidity", "smart contract", "blockchain", "defi"),
    "data": ("pandas", "numpy", "excel", "google sheets", "google таблицы", "csv", "tableau"),
    "browser automation": ("playwright", "selenium", "puppeteer"),
    "devops": ("linux", "nginx", "ci/cd", "github actions", "aws", "yandex cloud", "terraform"),
    "speech": ("whisper", "speech recognition", "ocr", "tts", "voice"),
    "documents": ("pdf", "docx", "document parsing", "excel"),
    "git": ("git", "github"),
    "email": ("outlook", "imap", "smtp", "email"),
    "payments": ("stripe", "yookassa", "payment", "billing"),
    "1c": ("1с", "1c"),
    "bitrix": ("bitrix", "битрикс"),
    "seo": ("seo",),
}

LANGUAGE_SKILLS = {
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "java": "java",
    "kotlin": "kotlin",
    "c#": "csharp",
    "c++": "cpp",
    "c": "c",
    "go": "go",
    "rust": "rust",
    "php": "php",
    "ruby": "ruby",
    "swift": "swift",
    "html": "html",
    "css": "css",
    "sql": "sql",
}

EXTENSION_SKILLS = {
    ".py": ("python",),
    ".ts": ("typescript",),
    ".tsx": ("typescript", "react"),
    ".js": ("javascript",),
    ".jsx": ("javascript", "react"),
    ".java": ("java",),
    ".kt": ("kotlin",),
    ".cs": ("csharp",),
    ".cpp": ("cpp",),
    ".cc": ("cpp",),
    ".c": ("c",),
    ".go": ("go",),
    ".rs": ("rust",),
    ".php": ("php",),
    ".rb": ("ruby",),
    ".swift": ("swift",),
    ".sql": ("sql",),
    ".html": ("html",),
    ".htm": ("html",),
    ".css": ("css",),
    ".scss": ("css",),
    ".vue": ("vue",),
    ".svelte": ("svelte",),
    ".tf": ("devops",),
    ".yml": ("devops",),
    ".yaml": ("devops",),
}

STOPWORD_OVERRIDES: dict[str, set[str]] = {
    "html": {"верстка", "фронтенд", "frontend"},
    "css": {"верстка", "фронтенд", "frontend"},
    "typescript": {"верстка", "фронтенд", "frontend"},
    "javascript": {"верстка", "фронтенд", "frontend"},
    "react": {"верстка", "фронтенд", "frontend"},
    "vue": {"верстка", "фронтенд", "frontend"},
    "svelte": {"верстка", "фронтенд", "frontend"},
    "php": {"php"},
    "laravel": {"php", "laravel"},
    "wordpress": {"wordpress", "вордпресс"},
    "crypto": {"крипта", "смарт-контракт", "solidity"},
    "seo": {"seo", "продвижение"},
    "1c": {"1с", "1c"},
    "bitrix": {"bitrix", "битрикс"},
}


@lru_cache(maxsize=2048)
def _alias_regex(alias: str) -> re.Pattern[str] | None:
    normalized = normalize(alias)
    if not normalized:
        return None
    return re.compile(
        rf"(?<![\w]){re.escape(normalized)}(?![\w])", re.IGNORECASE
    )


def extract_repo_skills(
    repo: dict[str, Any],
    documents: Iterable[dict[str, Any]] = (),
    *,
    extra_text: str = "",
) -> list[str]:
    """Extract canonical skills from language, topics, file paths and docs."""
    topics = repo.get("topics") or []
    if isinstance(topics, str):
        try:
            parsed = json.loads(topics)
            topics = parsed if isinstance(parsed, list) else [topics]
        except json.JSONDecodeError:
            topics = [topics]
    chunks: list[str] = [
        str(repo.get("description") or ""),
        str(repo.get("language") or ""),
        " ".join(str(topic) for topic in topics),
        str(repo.get("summary") or ""),
        extra_text,
    ]
    paths: list[str] = []
    for document in documents:
        path = str(document.get("path") or "")
        if path:
            paths.append(path)
        if document.get("kind") in {"readme", "manifest", "doc", "profile"}:
            chunks.append(str(document.get("content") or "")[:30_000])
    chunks.extend(paths)
    haystack = normalize("\n".join(chunks))
    skills: set[str] = set()

    language = str(repo.get("language") or "").strip().lower()
    if language in LANGUAGE_SKILLS:
        skills.add(LANGUAGE_SKILLS[language])

    for path in paths:
        lower = path.lower()
        for extension, mapped in EXTENSION_SKILLS.items():
            if lower.endswith(extension):
                skills.update(mapped)

    for skill, aliases in SKILL_ALIASES.items():
        if any((regex := _alias_regex(alias)) and regex.search(haystack) for alias in aliases):
            skills.add(skill)
    return sorted(skills)


def stopwords_for_skills(skills: Iterable[str]) -> set[str]:
    disabled: set[str] = set()
    for skill in skills:
        disabled.update(STOPWORD_OVERRIDES.get(skill, set()))
    return disabled


class SkillCatalog:
    """Global competence catalog derived from indexed GitHub repositories."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._skills: set[str] = set()
        self._keywords: list[str] = []
        self._disabled_stopwords: set[str] = set()

    async def ensure_backfilled(self) -> None:
        if await self.db.repo_skills_all():
            return
        await self.backfill_from_db()

    async def backfill_from_db(self) -> None:
        repos = {int(row["repo_id"]): row for row in await self.db.repo_list()}
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for document in await self.db.repo_documents_for_skills():
            grouped[int(document["repo_id"])].append(document)
        for repo_id, repo in repos.items():
            skills = extract_repo_skills(
                repo,
                grouped.get(repo_id, []),
                extra_text=str(repo.get("summary") or ""),
            )
            await self.db.repo_skills_replace(repo_id, skills)

    async def refresh(self) -> None:
        self._skills = set(await self.db.repo_skills_all())
        aliases: list[str] = []
        for skill in sorted(self._skills):
            aliases.extend(SKILL_ALIASES.get(skill, (skill,)))
        self._keywords = sorted(set(aliases))
        self._disabled_stopwords = stopwords_for_skills(self._skills)

    @property
    def skills(self) -> list[str]:
        return sorted(self._skills)

    @property
    def keywords(self) -> list[str]:
        return list(self._keywords)

    @property
    def disabled_stopwords(self) -> set[str]:
        return set(self._disabled_stopwords)
