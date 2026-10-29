from __future__ import annotations

import asyncio
import sys

from app.config import load_settings
from app.github_sync import GitHubClient


async def run() -> None:
    settings = load_settings()
    print(f"owner: {settings.github_owner}")
    print(f"token: {'configured' if settings.github_token else 'not configured (60 req/h)'}")
    client = GitHubClient(settings)
    try:
        rate = await client.rate_limit()
        core = rate.get("resources", {}).get("core", {}) if rate else {}
        if core:
            print(
                f"rate limit: {core.get('remaining')}/{core.get('limit')} "
                f"reset={core.get('reset')}"
            )
        repos = await client.list_repos()
        print(f"repositories: {len(repos)}")
        for repo in repos[:5]:
            print(f"  {repo.get('full_name')} | {repo.get('pushed_at') or '—'}")
        if len(repos) > 5:
            print(f"  ... and {len(repos) - 5} more")
    finally:
        await client.close()


def main() -> None:
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"GitHub check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
