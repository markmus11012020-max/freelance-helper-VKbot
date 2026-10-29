from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.config import load_settings
from app.db import Database
from app.embeddings import EmbeddingClient
from app.github_sync import GitHubSync
from app.llm import LlmClient
from app.repo_memory import RepoMemory

log = logging.getLogger("sync_repos")


async def run() -> None:
    parser = argparse.ArgumentParser(description="Sync GitHub repositories into vector memory")
    parser.add_argument("--list", action="store_true", help="list repositories and exit")
    parser.add_argument("--stats", action="store_true", help="show index stats and exit")
    parser.add_argument("--full", action="store_true", help="reindex even unchanged repositories")
    parser.add_argument("--repo", default="", help="only owner/name")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    settings = load_settings()
    db = Database(settings.database_path)
    await db.init()
    embeddings = EmbeddingClient(settings, db)
    memory = RepoMemory(settings, db, embeddings)
    await memory.load()
    llm = LlmClient(settings, db)
    sync = GitHubSync(settings, db, llm, embeddings, memory)
    try:
        if args.list:
            repos = await sync.client.list_repos()
            for repo in repos:
                print(
                    f"{repo.get('full_name')} | {repo.get('language') or '—'} | "
                    f"{repo.get('pushed_at') or '—'} | {repo.get('size') or 0} KB"
                )
            return
        if args.stats:
            print(json.dumps(await sync.stats(), ensure_ascii=False, indent=2))
            return
        only = {args.repo} if args.repo else None
        result = await sync.sync_all(only=only, force=args.full)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await sync.close()
        await llm.close()
        await embeddings.close()
        await db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
