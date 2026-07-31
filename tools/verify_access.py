#!/usr/bin/env python3
"""Tool-access levels and turn cancellation.

The point under test is that the two surfaces are gated separately: the
IP-whitelisted console and API keys valid from any address must not share one
switch.
"""
import asyncio
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="access-test-")
os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-acp")
os.environ["KIRO_BACKEND"] = "acp"
os.environ["USAGE_DB_PATH"] = os.path.join(TMP, "usage.db")
os.environ["ENV_FILE"] = os.path.join(TMP, ".env")
os.environ["KIRO_BRIDGE_WORKDIR"] = TMP
open(os.environ["ENV_FILE"], "w").write("TOOL_ACCESS=off\n")

from kiro_openai import acp  # noqa: E402
from kiro_openai.config import settings  # noqa: E402

failures = []

# Remember every agent process so cleanup can be asserted precisely.
spawned = []
_orig_start = acp.AcpClient.start


async def _tracked_start(self):
    await _orig_start(self)
    spawned.append(self)


acp.AcpClient.start = _tracked_start


def check(label, condition, detail=""):
    print("[{0}] {1}{2}".format("PASS" if condition else "FAIL", label,
                                "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


async def run(allow_tools):
    os.environ["ACP_DIALECT"] = "spec"
    os.environ["ACP_ASK_PERMISSION"] = "1"
    os.environ["ACP_PERM_LOG"] = os.path.join(TMP, "perm.txt")
    if os.path.exists(os.environ["ACP_PERM_LOG"]):
        os.remove(os.environ["ACP_PERM_LOG"])
    out = []
    async for kind, chunk in acp.stream_turn("do it", timeout=30, allow_tools=allow_tools):
        if kind == "text":
            out.append(chunk)
    reply = ""
    if os.path.exists(os.environ["ACP_PERM_LOG"]):
        reply = open(os.environ["ACP_PERM_LOG"]).read()
    return "".join(out), reply


async def main():
    print("== permission decision follows allow_tools ==")
    answer, reply = await run(allow_tools=False)
    check("turn still completes when declining", bool(answer), answer[:60])
    check("declines by default", '"optionId": "reject"' in reply or "reject" in reply, reply)

    answer, reply = await run(allow_tools=True)
    check("turn completes when allowing", bool(answer), answer[:60])
    check("approves when tools are enabled", "allow" in reply, reply)

    print("\n== access levels map to the right surfaces ==")
    for level, console, api in (("off", False, False), ("console", True, False), ("all", True, True)):
        settings.tool_access = level
        check("{0}: console={1}".format(level, console), settings.tools_for_console is console, level)
        check("{0}: api={1}".format(level, api), settings.tools_for_api is api, level)
    settings.tool_access = "off"

    print("\n== working directory ==")
    settings.tool_root = ""
    check("defaults to the scratch workdir", settings.agent_cwd == settings.workdir, settings.agent_cwd)
    settings.tool_root = TMP
    check("tool_root overrides it", settings.agent_cwd == TMP, settings.agent_cwd)
    settings.tool_root = ""

    print("\n== cancelling a turn ==")
    os.environ.pop("ACP_ASK_PERMISSION", None)
    os.environ["ACP_SLOW"] = "1"

    seen = []

    async def consume():
        async for kind, chunk in acp.stream_turn("long answer", timeout=30):
            seen.append(kind)

    task = asyncio.create_task(consume())
    await asyncio.sleep(1.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    check("chunks arrived before cancelling", len(seen) > 0, seen)
    check("cancelled before the answer finished", len(seen) < 6, seen)

    # Inspect the processes we actually spawned rather than grepping the
    # process table, which also matches the grep's own shell.
    await asyncio.sleep(0.6)
    alive = [c for c in spawned if c._proc is not None and c._proc.returncode is None]
    check("every agent process was reaped", not alive,
          "{0} of {1} still running".format(len(alive), len(spawned)))
    check("cancellation was actually exercised", len(spawned) >= 3, len(spawned))
    os.environ.pop("ACP_SLOW", None)

    print("\n" + "=" * 50)
    if failures:
        print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
        return 1
    print("ACCESS AND CANCELLATION CONFIRMED")
    return 0


sys.exit(asyncio.run(main()))
