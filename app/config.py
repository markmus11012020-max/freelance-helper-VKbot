from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _json_list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(value, list):
        return default
    return [str(item).strip() for item in value if str(item).strip()]


DEFAULT_KEYWORDS = [
    "telegram бот", "тг бот", "телеграм бот", "вк бот", "vk бот", "бот",
    "парсер", "парсинг", "скрипт", "автоматизация", "python",
    "интеграция api", "api интеграция", "google таблицы", "google sheets",
    "сбор данных", "webhook", "n8n", "ai агент", "ии агент", "gpt",
    "openai", "llm", "selenium", "playwright", "rest api", "fastapi",
    "excel", "csv",
]

DEFAULT_STOPWORDS = [
    "1с", "1c", "bitrix", "битрикс", "wordpress", "вордпресс", "php",
    "laravel", "opencart", "modx", "drupal", "joomla", "dle", "seo",
    "продвижение", "дизайн", "верстка", "фронтенд", "frontend", "крипта",
    "смарт-контракт", "solidity", "3d", "монтаж", "озвучка", "перевод",
    "копирайт", "статьи", "смм", "таргет", "бухгалтер", "юридическ",
    "курьер", "оператор", "инфографика", "логотип", "нейминг", "презентац",
    "тестирование", "тестировщик", "qa", "аудит", "вакансия", "резюме", "оклад",
    "полная занятость", "отзывы", "обзвон", "холодн", "продаж", "звонк",
]


@dataclass(frozen=True)
class Settings:
    # VK
    vk_group_token: str = ""
    vk_group_id: int = 0
    vk_api_version: str = "5.199"
    vk_chat_peer_id: int = 0
    vk_group_screen_name: str = ""
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)

    # Telegram (probe now, full channel later)
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_admin_user_ids: frozenset[int] = field(default_factory=frozenset)
    telegram_chat_id: int = 0
    telegram_proxy: str = ""
    telegram_api_url: str = "https://api.telegram.org"
    telegram_poll_timeout: int = 25

    # Runtime
    database_path: str = "data/bot.db"
    scan_interval_seconds: int = 600
    request_timeout_seconds: int = 25
    http_user_agent: str = "FreelanceMonitor/2.0 (personal research)"
    log_level: str = "INFO"
    health_port: int = 8080
    dry_run: bool = False
    tz: str = "Europe/Moscow"

    # Sources
    fl_ru_enabled: bool = True
    fl_ru_rss_url: str = "https://www.fl.ru/rss/all.xml"
    fl_ru_max_detail_fetch: int = 8
    freelance_ru_enabled: bool = True
    freelance_ru_url: str = "https://freelance.ru/projects/"
    freelance_ru_pages: int = 2
    kwork_enabled: bool = True
    kwork_url: str = "https://kwork.ru/projects"
    kwork_pages: int = 2

    # Selection
    keywords: tuple[str, ...] = tuple(DEFAULT_KEYWORDS)
    stopwords: tuple[str, ...] = tuple(DEFAULT_STOPWORDS)
    min_score: int = 48
    max_orders_per_cycle: int = 10
    budget_min_rub: int = 15000

    # LLM (OpenAI-compatible, e.g. aitunnel)
    openai_base_url: str = "https://api.aitunnel.ru/v1"
    openai_api_key: str = ""
    openai_model: str = "deepseek-v4.1-flash-1017"
    openai_temperature: float = 0.3
    openai_max_tokens: int = 900
    llm_enabled: bool = True
    llm_daily_token_limit: int = 300_000

    @property
    def vk_configured(self) -> bool:
        return bool(self.vk_group_token and self.vk_group_id)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def llm_configured(self) -> bool:
        return self.llm_enabled and bool(self.openai_api_key)

    @property
    def vk_chat_configured(self) -> bool:
        return bool(self.vk_chat_peer_id or self.admin_user_ids)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids


def load_settings() -> Settings:
    admin_ids = set()
    for raw in (_env("VK_ADMIN_USER_IDS") + "," + _env("VK_ADMIN_USER_ID")).split(","):
        raw = raw.strip()
        if raw.isdigit():
            admin_ids.add(int(raw))

    default_db = "/data/bot.db" if Path("/data").is_dir() else "data/bot.db"
    telegram_admin_ids = set()
    for raw in (_env("TELEGRAM_ADMIN_USER_IDS") + "," + _env("TELEGRAM_ADMIN_USER_ID")).split(","):
        raw = raw.strip()
        if raw.isdigit():
            telegram_admin_ids.add(int(raw))

    return Settings(
        vk_group_token=_env("VK_GROUP_TOKEN"),
        vk_group_id=_int("VK_GROUP_ID", 0),
        vk_api_version=_env("VK_API_VERSION", "5.199") or "5.199",
        vk_chat_peer_id=_int("VK_CHAT_PEER_ID", 0),
        vk_group_screen_name=_env("VK_GROUP_SCREEN_NAME"),
        admin_user_ids=frozenset(admin_ids),
        telegram_enabled=_bool("TELEGRAM_ENABLED", True),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_admin_user_ids=frozenset(telegram_admin_ids),
        telegram_chat_id=_int("TELEGRAM_CHAT_ID", 0),
        telegram_proxy=_env("TELEGRAM_PROXY"),
        telegram_api_url=_env("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/"),
        telegram_poll_timeout=_int("TELEGRAM_POLL_TIMEOUT", 25),
        database_path=_env("DATABASE_PATH", default_db) or default_db,
        scan_interval_seconds=_int("SCAN_INTERVAL_SECONDS", 600),
        request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 25),
        http_user_agent=_env("HTTP_USER_AGENT", "FreelanceMonitor/2.0 (personal research)"),
        log_level=_env("LOG_LEVEL", "INFO").upper() or "INFO",
        health_port=_int("HEALTH_PORT", 8080),
        dry_run=_bool("DRY_RUN", False),
        tz=_env("TZ", "Europe/Moscow") or "Europe/Moscow",
        fl_ru_enabled=_bool("FL_RU_ENABLED", True),
        fl_ru_rss_url=_env("FL_RU_RSS_URL", "https://www.fl.ru/rss/all.xml"),
        fl_ru_max_detail_fetch=_int("FL_RU_MAX_DETAIL_FETCH", 8),
        freelance_ru_enabled=_bool("FREELANCE_RU_ENABLED", True),
        freelance_ru_url=_env("FREELANCE_RU_URL", "https://freelance.ru/projects/"),
        freelance_ru_pages=_int("FREELANCE_RU_PAGES", 2),
        kwork_enabled=_bool("KWORK_ENABLED", True),
        kwork_url=_env("KWORK_URL", "https://kwork.ru/projects"),
        kwork_pages=_int("KWORK_PAGES", 2),
        keywords=tuple(_json_list("KEYWORDS_JSON", DEFAULT_KEYWORDS)),
        stopwords=tuple(_json_list("STOPWORDS_JSON", DEFAULT_STOPWORDS)),
        min_score=_int("MIN_SCORE", 48),
        max_orders_per_cycle=_int("MAX_ORDERS_PER_CYCLE", 10),
        budget_min_rub=_int("BUDGET_MIN_RUB", 15000),
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.aitunnel.ru/v1"),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_model=_env("OPENAI_MODEL", "deepseek-v4.1-flash-1017"),
        openai_temperature=_float("OPENAI_TEMPERATURE", 0.3),
        openai_max_tokens=_int("OPENAI_MAX_TOKENS", 900),
        llm_enabled=_bool("LLM_ENABLED", True),
        llm_daily_token_limit=_int("LLM_DAILY_TOKEN_LIMIT", 300_000),
    )
