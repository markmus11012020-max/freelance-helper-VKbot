"""Check Telegram Bot API connectivity before building the channel.

Usage:
    python -m scripts.check_telegram
    python -m scripts.check_telegram --attempts 5 --poll 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import load_settings
from app.telegram_probe import check_telegram


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram Bot API connectivity probe")
    parser.add_argument("--attempts", type=int, default=3, help="getMe attempts (1-5)")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout, seconds")
    parser.add_argument("--poll", type=int, default=6, help="getUpdates timeout, 0 = skip")
    parser.add_argument("--no-ip", action="store_true", help="do not request egress IP")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = load_settings()
    report = await check_telegram(
        settings,
        attempts=args.attempts,
        timeout=args.timeout,
        poll_timeout=args.poll,
        check_ip=not args.no_ip,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n--- summary ---")
    print(f"route:      {report.get('route')}")
    print(f"base_url:   {report.get('base_url')}")
    print(f"success:    {report.get('success')}/{len(report.get('attempts') or [])}")
    latencies = [item["latency_ms"] for item in report.get("attempts") or [] if item.get("ok")]
    if latencies:
        print(f"latency:    min={min(latencies)} ms, avg={round(sum(latencies) / len(latencies))} ms")
    poll = report.get("poll")
    if poll:
        print(f"getUpdates: ok={poll.get('ok')}, updates={poll.get('updates')}")
    if report.get("egress_ip"):
        print(f"egress IP:  {report['egress_ip']}")
    print(f"result:     {'OK' if report.get('ok') else 'FAILED — ' + str(report.get('error'))}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(asyncio.run(main()))
