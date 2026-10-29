from __future__ import annotations

import io
import tarfile
from dataclasses import replace

import pytest

from app.config import load_settings
from app.db import Database
from app.embeddings import EmbeddingClient
from app.github_sync import (
    GitHubClient,
    GitHubRateLimit,
    GitHubSync,
    chunk_text,
    kind_for_path,
    redact_secrets,
    safe_extract_files,
    should_index_path,
)
from app.repo_memory import RepoMemory


def make_tarball(files: dict[str, bytes], prefix: str = "owner-repo-abcdef1234567890") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_path_filter_and_kinds() -> None:
    assert should_index_path("README.md")
    assert should_index_path("src/bot.py")
    assert should_index_path("Dockerfile")
    assert should_index_path("docs/guide.md")
    assert not should_index_path(".env")
    assert not should_index_path("node_modules/pkg/index.js")
    assert not should_index_path("assets/logo.png")
    assert not should_index_path("data/table.csv")

    assert kind_for_path("README.md") == "readme"
    assert kind_for_path("src/bot.py") == "code"
    assert kind_for_path("docs/guide.md") == "doc"
    assert kind_for_path("requirements.txt") == "manifest"


def test_local_github_request_budget() -> None:
    settings = replace(load_settings(), github_max_requests_per_hour=2)
    client = GitHubClient(settings, session=object())  # type: ignore[arg-type]
    client._reserve_request()
    client._reserve_request()
    with pytest.raises(GitHubRateLimit):
        client._reserve_request()


def test_redact_secrets() -> None:
    text = (
        'api_key = "abcdefghijklmnop"\n'
        "token: ghp_abcdefghijklmnopqrstuvwxyz123456\n"
        "sk-abcdefghijklmnopqrstuvwxyz\n"
        "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----"
    )
    cleaned = redact_secrets(text)
    assert "abcdefghijklmnop" not in cleaned
    assert "ghp_" not in cleaned
    assert "sk-" not in cleaned
    assert "PRIVATE KEY" in cleaned
    assert "[REDACTED" in cleaned


def test_chunk_text_line_numbers() -> None:
    text = "\n".join(f"line {index}" for index in range(80))
    chunks = chunk_text(text, max_chars=120, overlap=20)
    assert len(chunks) > 1
    assert chunks[0][1] == 1
    assert chunks[-1][2] == 80
    assert all(chunk for chunk, _, _ in chunks)


def test_safe_extract_skips_env_and_binary() -> None:
    raw = make_tarball(
        {
            "README.md": b"# Bot\napi_key = \"abcdefghijklmnop\"",
            ".env": b"SECRET=should-not-be-indexed",
            "src/bot.py": b"print('hello')",
            "assets/logo.png": b"\x89PNG\x00binary",
            "node_modules/pkg/index.js": b"ignored",
        }
    )
    files, prefix = safe_extract_files(
        raw, max_files=20, max_file_bytes=10_000, max_total_bytes=100_000
    )
    assert prefix == "owner-repo-abcdef1234567890"
    paths = {item["path"] for item in files}
    assert paths == {"README.md", "src/bot.py"}
    assert "abcdefghijklmnop" not in files[0]["content"]


class NoEmbeddings:
    available = False

    @staticmethod
    def decode(blob, dimension):
        return EmbeddingClient.decode(blob, dimension)


class FakeLlm:
    available = True

    async def summarize_repo(self, repo, **kwargs):
        return "Проект: Telegram-бот для мониторинга заказов. Стек: Python."


class FakeGitHubClient:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.downloads = 0
        self.closed = False

    async def list_repos(self):
        return [
            {
                "id": 101,
                "full_name": "owner/repo",
                "html_url": "https://github.com/owner/repo",
                "description": "Telegram bot",
                "default_branch": "main",
                "language": "Python",
                "topics": ["telegram", "bot"],
                "pushed_at": "2026-10-29T00:00:00Z",
                "size": 12,
                "fork": False,
                "archived": False,
            }
        ]

    async def download_tarball(self, full_name: str, ref: str) -> bytes:
        self.downloads += 1
        return self.raw

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "github.db"))
    await database.init()
    yield database
    await database.close()


async def test_sync_indexes_new_repo_and_skips_unchanged(db: Database) -> None:
    settings = replace(
        load_settings(),
        github_repos=(),
        github_owner="owner",
        embedding_dimensions=3,
        github_max_files_per_repo=20,
        github_max_chunks_per_repo=50,
    )
    raw = make_tarball(
        {
            "README.md": b"# Bot\nTelegram bot for orders",
            "src/bot.py": b"def main():\n    print('hello')",
        }
    )
    client = FakeGitHubClient(raw)
    memory = RepoMemory(settings, db, NoEmbeddings())  # type: ignore[arg-type]
    sync = GitHubSync(settings, db, FakeLlm(), NoEmbeddings(), memory, client=client)  # type: ignore[arg-type]

    result = await sync.sync_all()
    assert result["indexed"] == 1
    assert result["seen"] == 1
    assert result["errors"] == 0
    assert client.downloads == 1
    repo = await db.repo_get(101)
    assert repo is not None
    assert repo["status"] == "indexed"
    assert repo["chunks"] >= 2
    docs = await db.repo_documents_all()
    assert any(doc["kind"] == "profile" for doc in docs)
    assert any(doc["path"] == "README.md" for doc in docs)

    again = await sync.sync_all()
    assert again["skipped"] == 1
    assert client.downloads == 1
    await sync.close()
    assert client.closed is True
