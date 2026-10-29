"""Check VK community token and Long Poll availability.

Usage:
    python -m scripts.check_vk
"""

from __future__ import annotations

import asyncio
import sys

from app.config import load_settings
from app.vk.client import VkClient, VkApiError


async def main() -> int:
    settings = load_settings()
    if not settings.vk_configured:
        print("VK_GROUP_TOKEN / VK_GROUP_ID are empty", file=sys.stderr)
        return 1
    print(f"group_id: {settings.vk_group_id}")
    vk = VkClient(settings)
    try:
        server = await vk.get_long_poll_server()
        print("long poll: OK")
        print(f"server: {str(server.get('server'))[:60]}")
        print(f"ts: {server.get('ts')}")
        try:
            group = await vk.call("groups.getById", group_id=settings.vk_group_id)
            if isinstance(group, list) and group:
                print(f"name: {group[0].get('name')}")
            elif isinstance(group, dict):
                print(f"name: {(group.get('groups') or [{}])[0].get('name')}")
        except VkApiError as exc:
            print(f"groups.getById: {exc}")
        return 0
    except VkApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await vk.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
