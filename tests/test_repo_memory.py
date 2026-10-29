from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import load_settings
from app.db import Database
from app.embeddings import EmbeddingClient
from app.repo_memory import RepoMemory


class FakeEmbeddings:
    available = True
    query = [1.0, 0.0, 0.0]

    @staticmethod
    def decode(blob, dimension):
        return EmbeddingClient.decode(blob, dimension)

    async def embed_one(self, text: str):
        return self.query


async def add_repo(db: Database, repo_id: int, name: str) -> None:
    await db.repo_upsert(
        {
            "repo_id": repo_id,
            "full_name": name,
            "url": f"https://github.com/{name}",
            "description": "test",
            "default_branch": "main",
            "language": "Python",
            "topics": [],
            "pushed_at": "2026-10-29T00:00:00Z",
        }
    )


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "repos.db"))
    await database.init()
    yield database
    await database.close()


async def test_vector_search_prefers_matching_repo(db: Database) -> None:
    settings = replace(load_settings(), embedding_dimensions=3, rag_min_score=0.0)
    await add_repo(db, 1, "owner/bot")
    await add_repo(db, 2, "owner/parser")
    await db.repo_replace_documents(
        1,
        "owner/bot",
        [
            {
                "kind": "profile",
                "path": "",
                "chunk_no": 0,
                "content": "Telegram bot",
                "content_hash": "a",
                "url": "https://github.com/owner/bot",
                "embedding": EmbeddingClient.encode([1.0, 0.0, 0.0]),
                "embedding_dim": 3,
            }
        ],
    )
    await db.repo_replace_documents(
        2,
        "owner/parser",
        [
            {
                "kind": "profile",
                "path": "",
                "chunk_no": 0,
                "content": "Parser",
                "content_hash": "b",
                "url": "https://github.com/owner/parser",
                "embedding": EmbeddingClient.encode([0.0, 1.0, 0.0]),
                "embedding_dim": 3,
            }
        ],
    )
    memory = RepoMemory(settings, db, FakeEmbeddings())  # type: ignore[arg-type]
    docs = await memory.search("telegram bot", top_k=2)
    assert docs
    assert docs[0]["repo_full_name"] == "owner/bot"
    assert "score" in docs[0]

    stats = await memory.stats()
    assert stats["chunks"] == 2
    assert stats["with_vectors"] == 2


async def test_lexical_fallback_works_without_embeddings(db: Database) -> None:
    settings = replace(load_settings(), embedding_dimensions=3, rag_min_score=0.2)
    await add_repo(db, 1, "owner/parser")
    await db.repo_replace_documents(
        1,
        "owner/parser",
        [
            {
                "kind": "readme",
                "path": "README.md",
                "chunk_no": 0,
                "content": "Парсер сайтов и сбор данных",
                "content_hash": "c",
                "url": "https://github.com/owner/parser",
            }
        ],
    )

    class NoEmbeddings:
        available = False

        @staticmethod
        def decode(blob, dimension):
            return EmbeddingClient.decode(blob, dimension)

    memory = RepoMemory(settings, db, NoEmbeddings())  # type: ignore[arg-type]
    docs = await memory.search("парсер", top_k=3)
    assert len(docs) == 1
    assert docs[0]["path"] == "README.md"


async def test_dedupe_by_repository(db: Database) -> None:
    settings = replace(load_settings(), embedding_dimensions=3, rag_min_score=0.0)
    await add_repo(db, 1, "owner/bot")
    await db.repo_replace_documents(
        1,
        "owner/bot",
        [
            {
                "kind": "profile",
                "path": "",
                "chunk_no": 0,
                "content": "Telegram bot profile",
                "content_hash": "a",
                "url": "https://github.com/owner/bot",
                "embedding": EmbeddingClient.encode([1.0, 0.0, 0.0]),
                "embedding_dim": 3,
            },
            {
                "kind": "code",
                "path": "bot.py",
                "chunk_no": 0,
                "content": "Telegram bot code",
                "content_hash": "b",
                "url": "https://github.com/owner/bot/blob/main/bot.py",
                "embedding": EmbeddingClient.encode([1.0, 0.0, 0.0]),
                "embedding_dim": 3,
            },
        ],
    )
    memory = RepoMemory(settings, db, FakeEmbeddings())  # type: ignore[arg-type]
    docs = await memory.search("telegram", top_k=5, dedupe_repos=True)
    assert len(docs) == 1
    assert docs[0]["kind"] == "profile"
