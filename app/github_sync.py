from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import tarfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from app.config import Settings
from app.db import Database
from app.embeddings import EmbeddingClient, EmbeddingError
from app.llm import LlmClient
from app.repo_memory import RepoMemory
from app.skills import extract_repo_skills
from app.textutil import truncate

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    ".md", ".markdown", ".rst", ".txt", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".java", ".kt", ".kts", ".cs", ".c", ".h", ".cpp", ".hpp", ".rs",
    ".rb", ".php", ".swift", ".scala", ".sql", ".sh", ".bash", ".ps1",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".html", ".css",
    ".scss", ".vue", ".svelte", ".dockerfile", ".tf",
}
MANIFEST_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile",
    "cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "gemfile", "poetry.lock", "uv.lock",
}
IGNORED_DIRS = {
    ".git", ".github", ".idea", ".vscode", "node_modules", "venv", ".venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist",
    "build", "target", "vendor", "coverage", ".next", ".nuxt", "site-packages",
}
IGNORED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar", ".exe", ".dll",
    ".so", ".dylib", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".mp3",
    ".mp4", ".mov", ".avi", ".wav", ".ogg", ".pyc", ".pyo", ".class",
    ".jar", ".war", ".lock", ".map", ".min.js", ".min.css", ".svg",
}
IGNORED_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development", "id_rsa",
    "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secrets",
}
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|client[_-]?secret|secret(?:[_-]?key)?|token|"
    r"password|passwd|access[_-]?key|private[_-]?key)"
    r"(\s*[:=]\s*)(['\"]?)([A-Za-z0-9_\-./+=]{12,})\3"
)
_TOKEN_RES = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
]


class GitHubError(RuntimeError):
    pass


class GitHubRateLimit(GitHubError):
    pass


class RepoTooLarge(GitHubError):
    pass


def _priority(path: str) -> tuple[int, int, str]:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name.startswith("readme"):
        return (0, len(path), path)
    if lower.startswith("docs/") or lower.endswith((".md", ".rst")):
        return (1, len(path), path)
    if name in MANIFEST_NAMES:
        return (2, len(path), path)
    if "/test" in lower or lower.startswith("test"):
        return (4, len(path), path)
    return (3, len(path), path)


def should_index_path(path: str) -> bool:
    lower = path.lower().replace("\\", "/")
    parts = lower.split("/")
    if any(part in IGNORED_DIRS for part in parts[:-1]):
        return False
    name = parts[-1]
    if name in IGNORED_NAMES or name.startswith(".env"):
        return False
    if any(lower.endswith(ext) for ext in IGNORED_EXTENSIONS):
        return False
    if name in MANIFEST_NAMES:
        return True
    return any(lower.endswith(ext) for ext in TEXT_EXTENSIONS)


def kind_for_path(path: str) -> str:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name.startswith("readme"):
        return "readme"
    if name in MANIFEST_NAMES:
        return "manifest"
    if lower.startswith("docs/") or lower.endswith((".md", ".markdown", ".rst", ".txt")):
        return "doc"
    return "code"


def language_for_path(path: str) -> str:
    name = path.lower().rsplit("/", 1)[-1]
    if name in {"dockerfile", "makefile"}:
        return name
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return {
        ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
        ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".java": "Java",
        ".kt": "Kotlin", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++",
        ".hpp": "C++", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
        ".swift": "Swift", ".scala": "Scala", ".sql": "SQL", ".sh": "Shell",
        ".ps1": "PowerShell", ".html": "HTML", ".css": "CSS", ".vue": "Vue",
        ".svelte": "Svelte", ".md": "Markdown", ".yaml": "YAML", ".yml": "YAML",
        ".toml": "TOML", ".json": "JSON", ".tf": "Terraform",
    }.get(ext, "")


def is_binary(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return True
    sample = data[:4096]
    if not sample:
        return False
    text_bytes = sum(32 <= byte <= 126 or byte in {9, 10, 13} or byte >= 192 for byte in sample)
    return text_bytes / len(sample) < 0.75


def redact_secrets(text: str) -> str:
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _SECRET_ASSIGN_RE.sub(r"\1\2\3[REDACTED]\3", text)
    for pattern in _TOKEN_RES:
        text = pattern.sub("[REDACTED TOKEN]", text)
    return text


def chunk_text(
    text: str, max_chars: int = 1400, overlap: int = 180
) -> list[tuple[str, int, int]]:
    """Split text into line-aware chunks with line numbers and overlap."""
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines):
            line_len = len(lines[end]) + 1
            if end > start and size + line_len > max_chars:
                break
            size += line_len
            end += 1
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append((chunk, start + 1, end))
        if end >= len(lines):
            break
        back = end
        overlap_size = 0
        while back > start + 1:
            line_len = len(lines[back - 1]) + 1
            if overlap_size + line_len > overlap:
                break
            overlap_size += line_len
            back -= 1
        start = max(back, start + 1)
    return chunks


def safe_extract_files(
    raw_tarball: bytes,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[list[dict[str, Any]], str]:
    """Read selected text files from a GitHub tarball without touching disk."""
    with tarfile.open(fileobj=io.BytesIO(raw_tarball), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile() and should_index_path(m.name)]
        if not members:
            return [], ""
        first_name = members[0].name
        prefix = first_name.split("/", 1)[0] if "/" in first_name else ""
        files: list[dict[str, Any]] = []
        total = 0
        for member in sorted(members, key=lambda item: _priority(item.name)):
            rel = member.name
            if prefix and rel.startswith(prefix + "/"):
                rel = rel[len(prefix) + 1:]
            if member.size > max_file_bytes:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            data = handle.read(max_file_bytes + 1)
            if len(data) > max_file_bytes or is_binary(data):
                continue
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                content = data.decode("utf-8", errors="replace")
            content = redact_secrets(content)
            if not content.strip():
                continue
            encoded_size = len(content.encode("utf-8"))
            if total + encoded_size > max_total_bytes:
                continue
            files.append(
                {
                    "path": rel,
                    "content": content,
                    "kind": kind_for_path(rel),
                    "language": language_for_path(rel),
                }
            )
            total += encoded_size
            if len(files) >= max_files:
                break
        root_prefix = prefix if any(m.name.startswith(prefix + "/") for m in members) else ""
        return files, root_prefix


class GitHubClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None) -> None:
        self.settings = settings
        self._owns_session = session is None
        self._session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        )
        self._request_times: deque[float] = deque()

    def _reserve_request(self) -> None:
        """Keep a safety margin below GitHub's unauthenticated 60/hour limit."""
        limit = self.settings.github_max_requests_per_hour
        if limit <= 0:
            return
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= 3600:
            self._request_times.popleft()
        if len(self._request_times) >= limit:
            raise GitHubRateLimit(
                f"local GitHub request budget reached ({limit}/hour)"
            )
        self._request_times.append(now)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.settings.http_user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return headers

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self._reserve_request()
        async with self._session.get(url, params=params, headers=self._headers()) as response:
            if response.status == 403 or response.status == 429:
                text = await response.text()
                raise GitHubError(f"GitHub rate limit/forbidden ({response.status}): {text[:300]}")
            if response.status >= 400:
                text = await response.text()
                raise GitHubError(f"GitHub HTTP {response.status}: {text[:300]}")
            return await response.json(content_type=None)

    async def list_repos(self) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._get_json(
                f"{self.settings.github_api_url}/users/{quote(self.settings.github_owner)}/repos",
                params={
                    "per_page": 100,
                    "page": page,
                    "sort": "pushed",
                    "direction": "desc",
                    "type": "owner",
                },
            )
            if not isinstance(data, list) or not data:
                break
            repos.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                break
            page += 1
        return repos

    async def rate_limit(self) -> dict[str, Any]:
        data = await self._get_json(f"{self.settings.github_api_url}/rate_limit")
        return data if isinstance(data, dict) else {}

    async def download_tarball(self, full_name: str, ref: str) -> bytes:
        url = (
            f"{self.settings.github_api_url}/repos/"
            f"{quote(full_name, safe='/')}/tarball/{quote(ref, safe='')}"
        )
        max_bytes = max(1, self.settings.github_max_repo_mb) * 1024 * 1024
        self._reserve_request()
        async with self._session.get(url, headers=self._headers()) as response:
            if response.status in {403, 429}:
                text = await response.text()
                raise GitHubRateLimit(f"GitHub rate limit ({response.status}): {text[:300]}")
            if response.status >= 400:
                text = await response.text()
                raise GitHubError(f"tarball HTTP {response.status}: {text[:300]}")
            try:
                length = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length and length > max_bytes:
                raise RepoTooLarge(f"repository tarball is {length // 1024} KB")
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    raise RepoTooLarge(f"repository tarball exceeds {max_bytes // 1024} KB")
            return bytes(buffer)

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()


class RepoIndexer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: LlmClient,
        embeddings: EmbeddingClient,
        memory: RepoMemory,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.embeddings = embeddings
        self.memory = memory

    async def index_repo(self, repo: dict[str, Any], raw_tarball: bytes) -> dict[str, Any]:
        files, root_prefix = safe_extract_files(
            raw_tarball,
            max_files=self.settings.github_max_files_per_repo,
            max_file_bytes=self.settings.github_max_file_kb * 1024,
            max_total_bytes=self.settings.github_max_total_kb_per_repo * 1024,
        )
        if not files:
            raise GitHubError("no text files suitable for indexing")

        commit = _commit_from_prefix(root_prefix) or str(repo.get("pushed_at") or "unknown")
        full_name = str(repo["full_name"])
        html_url = str(repo.get("url") or f"https://github.com/{full_name}")
        readme = next(
            (item["content"] for item in files if item["kind"] == "readme"), ""
        )
        manifests = "\n\n".join(
            f"### {item['path']}\n{item['content']}"
            for item in files
            if item["kind"] == "manifest"
        )
        summary = ""
        if self.settings.github_summary_enabled and self.llm.available:
            try:
                summary = await self.llm.summarize_repo(
                    repo,
                    readme=readme,
                    files=[item["path"] for item in files],
                    manifests=manifests,
                ) or ""
            except Exception:
                log.exception("repo summary failed for %s", full_name)
        if not summary:
            summary = _fallback_summary(repo, readme, [item["path"] for item in files])
            log.info("repo summary fallback used for %s", full_name)

        documents: list[dict[str, Any]] = [
            {
                "kind": "profile",
                "path": "",
                "chunk_no": 0,
                "content": summary,
                "content_hash": _hash(summary),
                "url": html_url,
                "language": str(repo.get("language") or ""),
                "commit_sha": commit,
                "token_count": max(1, len(summary) // 4),
            }
        ]

        for item in files:
            for chunk_no, (chunk, line_start, line_end) in enumerate(
                chunk_text(item["content"])
            ):
                documents.append(
                    {
                        "kind": item["kind"],
                        "path": item["path"],
                        "chunk_no": chunk_no,
                        "content": chunk,
                        "content_hash": _hash(chunk),
                        "url": (
                            f"https://github.com/{full_name}/blob/{commit}/"
                            f"{quote(item['path'], safe='/')}#L{line_start}-L{line_end}"
                        ),
                        "language": item["language"],
                        "line_start": line_start,
                        "line_end": line_end,
                        "commit_sha": commit,
                        "token_count": max(1, len(chunk) // 4),
                    }
                )

        documents = documents[: self.settings.github_max_chunks_per_repo]
        embedded = await self._embed_documents(documents)

        await self.db.repo_replace_documents(int(repo["repo_id"]), full_name, documents)
        await self.db.repo_set_indexed(
            int(repo["repo_id"]),
            commit,
            summary,
            len(documents),
            status="indexed" if embedded else "partial",
        )
        skills = extract_repo_skills(repo, files, extra_text=summary)
        await self.db.repo_skills_replace(int(repo["repo_id"]), skills)
        self.memory.mark_dirty()
        log.info(
            "indexed %s: %s files, %s chunks, commit=%s%s",
            full_name,
            len(files),
            len(documents),
            commit[:8],
            "" if embedded else " (lexical only)",
        )
        return {
            "full_name": full_name,
            "files": len(files),
            "chunks": len(documents),
            "commit": commit,
        }

    async def _embed_documents(self, documents: list[dict[str, Any]]) -> bool:
        if not self.embeddings.available:
            return True
        try:
            vectors = await self.embeddings.embed([item["content"] for item in documents])
        except EmbeddingError as exc:
            log.warning("repo embeddings failed: %s", exc)
            return False
        except Exception:
            log.exception("repo embeddings crashed")
            return False
        if any(not self.embeddings.validate_dimension(vector) for vector in vectors):
            log.warning(
                "embedding dimension does not match configured %s; storing lexical index only",
                self.settings.embedding_dimensions,
            )
            return False
        for item, vector in zip(documents, vectors):
            item["embedding"] = self.embeddings.encode(vector)
            item["embedding_dim"] = len(vector)
        return True


class GitHubSync:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: LlmClient,
        embeddings: EmbeddingClient,
        memory: RepoMemory,
        client: GitHubClient | None = None,
        on_sync: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.embeddings = embeddings
        self.memory = memory
        self.client = client or GitHubClient(settings)
        self.on_sync = on_sync
        self.indexer = RepoIndexer(settings, db, llm, embeddings, memory)
        self._lock = asyncio.Lock()
        self._last_result: dict[str, Any] = {}

    async def sync_all(
        self, *, only: set[str] | None = None, force: bool = False
    ) -> dict[str, Any]:
        if self._lock.locked():
            return {"busy": True, **self._last_result}
        async with self._lock:
            result: dict[str, Any] = {
                "seen": 0,
                "indexed": 0,
                "skipped": 0,
                "errors": 0,
                "missing": 0,
                "busy": False,
                "rate_limited": False,
            }
            try:
                repos = await self.client.list_repos()
            except Exception as exc:
                log.exception("GitHub repository list failed")
                await self.db.kv_set("github_last_error", str(exc)[:500])
                raise

            listed_ids = {
                int(repo["id"]) for repo in repos if isinstance(repo.get("id"), int)
            }
            if only:
                only_lower = {name.lower() for name in only}
                repos = [
                    repo for repo in repos
                    if str(repo.get("full_name") or "").lower() in only_lower
                ]
            repos = self._filter_repos(repos)
            result["seen"] = len(repos)

            stored_spec = await self.db.kv_get("embedding_spec", "")
            if self.embeddings.available and stored_spec != self.embeddings.spec:
                if stored_spec:
                    log.warning(
                        "embedding spec changed from %s to %s; full reindex",
                        stored_spec,
                        self.embeddings.spec,
                    )
                else:
                    log.info("building vector index with %s", self.embeddings.spec)
                force = True

            for repo in repos:
                repo_id = int(repo["id"])
                meta = self._normalize_repo(repo)
                existing = await self.db.repo_get(repo_id)
                await self.db.repo_upsert(meta)
                if (
                    not force
                    and existing
                    and existing.get("status") == "indexed"
                    and existing.get("commit_sha")
                    and existing.get("full_name") == meta.get("full_name")
                    and existing.get("default_branch") == meta.get("default_branch")
                    and existing.get("description") == meta.get("description")
                    and existing.get("language") == meta.get("language")
                    and existing.get("pushed_at") == meta.get("pushed_at")
                ):
                    result["skipped"] += 1
                    continue
                await self.db.repo_set_status(repo_id, "indexing")
                try:
                    raw = await self.client.download_tarball(
                        str(repo["full_name"]), str(repo.get("default_branch") or "main")
                    )
                    indexed = await self.indexer.index_repo(meta, raw)
                    result["indexed"] += 1
                    log.info("GitHub sync: %s", indexed)
                except GitHubRateLimit as exc:
                    result["errors"] += 1
                    result["rate_limited"] = True
                    await self.db.repo_set_status(repo_id, "error", str(exc))
                    log.warning("GitHub rate limit reached; stopping sync: %s", exc)
                    break
                except RepoTooLarge as exc:
                    result["skipped"] += 1
                    await self.db.repo_set_status(repo_id, "skipped", str(exc))
                    log.warning("skip %s: %s", repo["full_name"], exc)
                except Exception as exc:
                    result["errors"] += 1
                    await self.db.repo_set_status(repo_id, "error", str(exc))
                    log.exception("indexing %s failed", repo["full_name"])

            if only is None:
                for row in await self.db.repo_list():
                    if row["status"] != "missing" and int(row["repo_id"]) not in listed_ids:
                        await self.db.repo_mark_missing(int(row["repo_id"]))
                        result["missing"] += 1

            now = datetime.now(timezone.utc).isoformat()
            await self.db.kv_set("github_last_sync", now)
            await self.db.kv_set(
                "github_last_error",
                "GitHub rate limit; add GITHUB_TOKEN or retry later"
                if result["rate_limited"]
                else "",
            )
            if self.embeddings.available:
                await self.db.kv_set("embedding_spec", self.embeddings.spec)
            result["last_sync"] = now
            self._last_result = result
            self.memory.mark_dirty()
            if self.on_sync is not None:
                try:
                    await self.on_sync()
                except Exception:
                    log.exception("post-sync callback failed")
            log.info("GitHub sync done: %s", result)
            return result

    def _filter_repos(self, repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allow = {name.lower() for name in self.settings.github_repos}
        selected = []
        for repo in repos:
            full_name = str(repo.get("full_name") or "")
            if not full_name:
                continue
            if allow and full_name.lower() not in allow:
                continue
            if repo.get("private") and not self.settings.github_include_private:
                continue
            if repo.get("fork") and not self.settings.github_include_forks:
                continue
            if repo.get("archived") and not self.settings.github_include_archived:
                continue
            if int(repo.get("size") or 0) > self.settings.github_max_repo_mb * 1024:
                log.warning("skip %s: repo is too large (%s KB)", full_name, repo.get("size"))
                continue
            if not repo.get("default_branch"):
                continue
            selected.append(repo)
        return selected

    @staticmethod
    def _normalize_repo(repo: dict[str, Any]) -> dict[str, Any]:
        return {
            "repo_id": int(repo["id"]),
            "full_name": str(repo.get("full_name") or ""),
            "url": str(repo.get("html_url") or ""),
            "description": str(repo.get("description") or ""),
            "default_branch": str(repo.get("default_branch") or ""),
            "language": str(repo.get("language") or ""),
            "topics": list(repo.get("topics") or []),
            "pushed_at": repo.get("pushed_at"),
            "size_kb": int(repo.get("size") or 0),
            "fork": bool(repo.get("fork")),
            "archived": bool(repo.get("archived")),
        }

    async def stats(self) -> dict[str, Any]:
        stats = await self.db.repo_stats()
        stats.update(
            {
                "last_sync": await self.db.kv_get("github_last_sync", ""),
                "last_error": await self.db.kv_get("github_last_error", ""),
                "busy": self._lock.locked(),
                "owner": self.settings.github_owner,
                "last_result": self._last_result,
            }
        )
        return stats

    async def run_forever(self, initial_delay: int = 20) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await self.sync_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("GitHub sync cycle failed")
            await asyncio.sleep(max(60, self.settings.github_sync_interval_seconds))

    async def close(self) -> None:
        await self.client.close()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _commit_from_prefix(prefix: str) -> str:
    match = re.search(r"-([0-9a-f]{7,40})$", prefix.lower())
    return match.group(1) if match else ""


def _fallback_summary(
    repo: dict[str, Any], readme: str, files: list[str] | None = None
) -> str:
    lines = [
        f"{repo.get('full_name')} — {repo.get('description') or 'проект без описания'}.",
    ]
    if repo.get("language"):
        lines.append(f"Стек: {repo['language']}.")
    if files:
        visible = [path for path in files if path and not path.startswith(("test", "tests/"))]
        if visible:
            lines.append("Файлы проекта: " + ", ".join(visible[:12]) + ".")
    if readme:
        lines.append(truncate(readme, 900))
    return "\n".join(lines)
