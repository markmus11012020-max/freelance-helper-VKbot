"""Check that the OpenAI-compatible provider answers with the configured model.

Usage:
    python -m scripts.check_llm
"""

from __future__ import annotations

import asyncio
import sys

from openai import AsyncOpenAI

from app.config import load_settings


async def main() -> int:
    settings = load_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is empty", file=sys.stderr)
        return 1
    print(f"base_url: {settings.openai_base_url}")
    print(f"model:    {settings.openai_model}")
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=60.0,
        max_retries=0,
    )
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": "Ответь одним словом: работает"}],
            max_tokens=200,
            temperature=0,
        )
        usage = response.usage
        message = response.choices[0].message
        answer = (message.content or "").strip()
        reasoning = getattr(message, "reasoning", None) or ""
        print("answer:   ", answer or "<empty>")
        if not answer and reasoning:
            print("reasoning:", str(reasoning)[:200])
        if usage is not None:
            print(f"tokens:    prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
        return 0 if answer else 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
