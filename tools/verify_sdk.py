#!/usr/bin/env python3
"""Prove the bridge works through the official `openai` SDK, not just raw HTTP."""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-kiro-cli")
os.environ["BRIDGE_API_KEY"] = "sk-test-123"

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from kiro_openai.server import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print("[{0}] {1}{2}".format("PASS" if condition else "FAIL", label,
                                "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


async def main():
    # Run the ASGI app in-process; identical code path to `uvicorn`.
    async with app.router.lifespan_context(app):
        http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://bridge")
        client = AsyncOpenAI(api_key="sk-test-123", base_url="http://bridge/v1",
                             http_client=http_client)

        print("== client.models.list() ==")
        models = await client.models.list()
        ids = [m.id for m in models.data]
        print(ids)
        check("SDK parsed model list", "claude-sonnet-4.5" in ids, ids)

        print("\n== client.chat.completions.create() ==")
        completion = await client.chat.completions.create(
            model="claude-sonnet-4.5",
            messages=[
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            temperature=0.3,
            max_tokens=100,
        )
        print(completion.choices[0].message.content)
        check("SDK deserialised completion", completion.object == "chat.completion")
        check("model echoed", completion.model == "claude-sonnet-4.5", completion.model)
        check("content non-empty", bool(completion.choices[0].message.content))
        check("unsupported params accepted without error", True)

        print("\n== streaming via SDK ==")
        pieces = []
        stream = await client.chat.completions.create(
            model="auto",
            messages=[{"role": "user", "content": "count"}],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                pieces.append(delta)
        joined = "".join(pieces)
        print(repr(joined[:100]))
        check("SDK consumed SSE stream", len(pieces) > 3, len(pieces))
        check("streamed text reassembled", "SAW_PLAIN" in joined, joined[:80])

        print("\n== bad credentials ==")
        bad = AsyncOpenAI(api_key="wrong", base_url="http://bridge/v1",
                         http_client=http_client)
        try:
            await bad.chat.completions.create(
                model="auto", messages=[{"role": "user", "content": "hi"}])
            check("SDK raises AuthenticationError", False, "no exception raised")
        except Exception as exc:  # noqa: BLE001
            check("SDK raises AuthenticationError", type(exc).__name__ == "AuthenticationError",
                  type(exc).__name__)

        await http_client.aclose()

    print("\n" + "=" * 46)
    if failures:
        print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
        return 1
    print("SDK COMPATIBILITY CONFIRMED")
    return 0


sys.exit(asyncio.run(main()))
