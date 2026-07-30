#!/usr/bin/env python3
"""ACP client: live streaming of reasoning and answer, in both wire dialects."""
import asyncio
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-acp")
os.environ["KIRO_BRIDGE_WORKDIR"] = tempfile.mkdtemp(prefix="acp-test-")

from kiro_openai import acp  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print("[{0}] {1}{2}".format("PASS" if condition else "FAIL", label,
                                "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


async def collect(dialect, **env):
    os.environ["ACP_DIALECT"] = dialect
    for key in ("ACP_NO_THOUGHT", "ACP_ASK_PERMISSION", "ACP_REFUSE", "ACP_NO_SET_MODEL"):
        os.environ.pop(key, None)
    os.environ.update({k: "1" for k in env})

    order, thoughts, answer = [], [], []
    async for kind, chunk in acp.stream_turn("compare sqlite and postgres",
                                             model="claude-sonnet-5", timeout=30):
        order.append(kind)
        (thoughts if kind == "thought" else answer).append(chunk)
    return order, "".join(thoughts), "".join(answer)


async def main():
    for dialect in ("spec", "kiro"):
        print("\n== {0} dialect ==".format(dialect))
        order, thinking, answer = await collect(dialect)

        check("{0}: reasoning streamed".format(dialect), bool(thinking), thinking[:60])
        check("{0}: reasoning arrives in several chunks".format(dialect),
              order.count("thought") >= 3, order[:6])
        check("{0}: answer streamed".format(dialect), bool(answer), answer[:60])
        check("{0}: answer arrives in several chunks".format(dialect),
              order.count("text") >= 5, order)
        check("{0}: reasoning precedes the answer".format(dialect),
              order.index("thought") < order.index("text"), order[:8])
        check("{0}: reasoning text intact".format(dialect),
              "clearest shape" in thinking, thinking)

        # The whole point of ACP: markdown source survives, unrendered.
        check("{0}: heading syntax preserved".format(dialect), "# Storage comparison" in answer, answer[:80])
        check("{0}: bold syntax preserved".format(dialect), "**concurrency**" in answer, answer[:120])
        check("{0}: table pipes preserved".format(dialect), "| Engine | Writes | Cost |" in answer, answer)
        check("{0}: table alignment row preserved".format(dialect), "| --- | ---: | :-: |" in answer, answer)
        check("{0}: fence preserved".format(dialect), "```python" in answer, answer[-200:])
        check("{0}: task list preserved".format(dialect), "- [x] Benchmark" in answer, answer)
        check("{0}: quote preserved".format(dialect), "> Measure first." in answer, answer)
        check("{0}: ordered list preserved".format(dialect), "1. Start with SQLite" in answer, answer)

    print("\n== reasoning can be absent ==")
    order, thinking, answer = await collect("spec", ACP_NO_THOUGHT="1")
    check("no thought chunks", thinking == "", thinking)
    check("answer still streams", bool(answer), answer[:60])

    print("\n== agent-initiated permission request ==")
    # Without a reply the turn would deadlock, so this proves we answer it.
    order, thinking, answer = await collect("spec", ACP_ASK_PERMISSION="1")
    check("turn completes despite a permission prompt", bool(answer), answer[:60])

    print("\n== unsupported session/set_model is survivable ==")
    order, thinking, answer = await collect("spec", ACP_NO_SET_MODEL="1")
    check("answer still streams when set_model is rejected", bool(answer), answer[:60])

    print("\n== refusal surfaces as an error ==")
    try:
        await collect("spec", ACP_REFUSE="1")
        check("refusal raises", False, "no exception")
    except acp.AcpError as exc:
        check("refusal raises AcpError", "refus" in str(exc).lower(), str(exc))

    print("\n== missing binary is reported clearly ==")
    os.environ["KIRO_CLI_BIN"] = "/nonexistent/acp-binary"
    from kiro_openai.config import settings
    settings.cli_bin = "/nonexistent/acp-binary"
    try:
        async for _ in acp.stream_turn("hi", timeout=10):
            pass
        check("missing binary raises", False, "no exception")
    except acp.AcpError as exc:
        check("missing binary raises AcpError", "cannot execute" in str(exc), str(exc))

    print("\n" + "=" * 50)
    if failures:
        print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
        return 1
    print("ACP CLIENT CONFIRMED")
    return 0


sys.exit(asyncio.run(main()))
