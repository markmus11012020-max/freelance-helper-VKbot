from __future__ import annotations

from dataclasses import replace

from app.config import load_settings
from app.filters import OrderFilter
from app.models import ParsedOrder
from app.skills import extract_repo_skills, stopwords_for_skills


def test_extract_skills_from_repo_metadata_and_docs() -> None:
    repo = {
        "language": "Python",
        "description": "Telegram bot for freelance orders",
        "topics": ["openai", "automation"],
    }
    documents = [
        {
            "kind": "readme",
            "path": "README.md",
            "content": "FastAPI service with SQLite and Playwright scraping.",
        },
        {
            "kind": "manifest",
            "path": "requirements.txt",
            "content": "fastapi\nplaywright\nopenai",
        },
        {"kind": "code", "path": "bot.py", "content": "import asyncio"},
    ]
    skills = set(extract_repo_skills(repo, documents))
    assert "python" in skills
    assert "telegram bot" in skills
    assert "llm" in skills
    assert "fastapi" in skills
    assert "sql" in skills
    assert "browser automation" in skills


def test_skill_stopword_overrides() -> None:
    disabled = stopwords_for_skills({"html", "php", "crypto"})
    assert "верстка" in disabled
    assert "php" in disabled
    assert "крипта" in disabled


def test_order_filter_refreshes_dynamic_keywords() -> None:
    settings = replace(
        load_settings(),
        keywords=("python",),
        stopwords=("верстка",),
        min_score=25,
    )
    flt = OrderFilter(settings, extra_keywords=["react"], disabled_stopwords=[])
    order = ParsedOrder(
        source="fl.ru",
        external_id="1",
        url="https://example.com/1",
        title="Нужен разработчик интерфейса",
        description="React и верстка личного кабинета.",
        budget_text="30 000 ₽",
        budget_min=30000,
        budget_unit="project",
    )
    assert flt.prefilter(order)

    flt.refresh(extra_keywords=[], disabled_stopwords=[])
    assert not flt.prefilter(order)
