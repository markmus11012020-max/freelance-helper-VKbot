from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from app.config import Settings

log = logging.getLogger(__name__)


class TelegramProbeError(RuntimeError):
    pass


def _api_url(settings: Settings, method: str) -> str:
    return f"{settings.telegram_api_url}/bot{settings.telegram_bot_token}/{method}"


def _proxy_kwargs(settings: Settings) -> dict[str, Any]:
    if not settings.telegram_proxy:
        return {}
    if settings.telegram_proxy.lower().startswith("socks"):
        raise TelegramProbeError(
            "probe поддерживает только HTTP-прокси; для SOCKS нужен полный модуль"
        )
    return {"proxy": settings.telegram_proxy}


async def _api_call(
    session: aiohttp.ClientSession,
    settings: Settings,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with session.post(
            _api_url(settings, method),
            data=params or {},
            timeout=aiohttp.ClientTimeout(total=timeout),
            **_proxy_kwargs(settings),
        ) as response:
            latency_ms = round((time.perf_counter() - started) * 1000)
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                payload = {}
            if response.status >= 400 or not payload.get("ok"):
                return {
                    "ok": False,
                    "latency_ms": latency_ms,
                    "error": str(payload.get("description") or f"HTTP {response.status}"),
                    "error_code": payload.get("error_code"),
                }
            return {"ok": True, "latency_ms": latency_ms, "result": payload.get("result")}
    except TelegramProbeError:
        raise
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": f"timeout after {timeout:.0f}s",
        }
    except aiohttp.ClientError as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


async def _egress_ip(session: aiohttp.ClientSession, settings: Settings) -> str | None:
    try:
        async with session.get(
            "https://api.ipify.org?format=json",
            timeout=aiohttp.ClientTimeout(total=5),
            **_proxy_kwargs(settings),
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)
            return str(payload.get("ip") or "") or None
    except Exception:
        return None


async def check_telegram(
    settings: Settings,
    *,
    attempts: int = 3,
    timeout: float = 10.0,
    poll_timeout: int = 6,
    check_ip: bool = True,
) -> dict[str, Any]:
    """Check Telegram Bot API reachability through the configured route."""
    attempts = max(1, min(5, int(attempts)))
    report: dict[str, Any] = {
        "ok": False,
        "token_configured": bool(settings.telegram_bot_token),
        "base_url": settings.telegram_api_url,
        "route": settings.telegram_proxy or "direct",
        "attempts": [],
        "success": 0,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    if not settings.telegram_bot_token:
        report["error"] = "TELEGRAM_BOT_TOKEN is empty"
        return report

    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=None),
    ) as session:
        for index in range(attempts):
            result = await _api_call(session, settings, "getMe", timeout=timeout)
            entry = {"attempt": index + 1, "ok": result["ok"], "latency_ms": result["latency_ms"]}
            if result["ok"]:
                me = result.get("result") or {}
                entry["bot"] = {
                    "id": me.get("id"),
                    "username": me.get("username"),
                    "first_name": me.get("first_name"),
                    "can_join_groups": me.get("can_join_groups"),
                    "can_read_all_group_messages": me.get("can_read_all_group_messages"),
                }
            else:
                entry["error"] = result.get("error")
                entry["error_code"] = result.get("error_code")
            report["attempts"].append(entry)
            if result["ok"]:
                report["success"] += 1
            if index < attempts - 1:
                await asyncio.sleep(0.4)

        if report["success"] and poll_timeout > 0:
            poll = await _api_call(
                session,
                settings,
                "getUpdates",
                {
                    "timeout": max(0, min(25, int(poll_timeout))),
                    "allowed_updates": json.dumps(["message", "callback_query", "chat_join_request"]),
                },
                timeout=max(5.0, poll_timeout + 8.0),
            )
            if poll["ok"]:
                updates = poll.get("result") or []
                update_ids = [int(item.get("update_id", 0)) for item in updates if item.get("update_id")]
                report["poll"] = {
                    "ok": True,
                    "latency_ms": poll["latency_ms"],
                    "updates": len(updates),
                    "types": sorted({str(item.get("type") or _update_kind(item)) for item in updates}),
                    "last_update_id": max(update_ids) if update_ids else None,
                }
            else:
                report["poll"] = {
                    "ok": False,
                    "latency_ms": poll["latency_ms"],
                    "error": poll.get("error"),
                    "error_code": poll.get("error_code"),
                }

        if check_ip:
            report["egress_ip"] = await _egress_ip(session, settings)

    all_ok = report["success"] == attempts
    poll_ok = report.get("poll", {}).get("ok", True)
    report["ok"] = bool(all_ok and poll_ok)
    if not report["ok"]:
        errors = [entry.get("error") for entry in report["attempts"] if entry.get("error")]
        if report.get("poll", {}).get("error"):
            errors.append(report["poll"]["error"])
        report["error"] = "; ".join(dict.fromkeys(str(error) for error in errors if error)) or "probe failed"
    return report


def _update_kind(update: dict[str, Any]) -> str:
    for key in ("message", "edited_message", "callback_query", "chat_join_request", "my_chat_member"):
        if key in update:
            return key
    return "unknown"


def build_probe_app(settings: Settings) -> web.Application:
    app = web.Application()

    async def healthz(request: web.Request) -> web.Response:
        def int_param(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(request.query.get(name, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        attempts = int_param("attempts", 3, 1, 5)
        poll_timeout = int_param("poll", 4, 0, 25)
        timeout = int_param("timeout", 6, 3, 30)
        check_ip = request.query.get("ip", "1") not in {"0", "false", "no"}
        try:
            report = await check_telegram(
                settings,
                attempts=attempts,
                timeout=float(timeout),
                poll_timeout=poll_timeout,
                check_ip=check_ip,
            )
        except TelegramProbeError as exc:
            report = {
                "ok": False,
                "token_configured": bool(settings.telegram_bot_token),
                "base_url": settings.telegram_api_url,
                "route": settings.telegram_proxy or "direct",
                "error": str(exc),
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        log.info(
            "telegram probe: ok=%s success=%s/%s route=%s",
            report.get("ok"),
            report.get("success"),
            attempts,
            report.get("route"),
        )
        return web.json_response(report, headers={"Cache-Control": "no-store"})

    async def root(_: web.Request) -> web.Response:
        return web.Response(
            text=(
                "Telegram probe is running.\n\n"
                "GET /healthz        - full check (3 attempts + getUpdates)\n"
                "GET /healthz?attempts=1&poll=0&ip=0 - quick check\n"
            ),
            content_type="text/plain",
        )

    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/health", healthz)
    app.router.add_get("/probe", healthz)
    return app


async def run_probe_server(settings: Settings) -> None:
    app = build_probe_app(settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    log.info("telegram probe listening on :%s", settings.health_port)
    log.info("route: %s", settings.telegram_proxy or "direct")
    log.info("base_url: %s", settings.telegram_api_url)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
