#!/usr/bin/env python3
"""Per-request model selection, and discovery from the CLI's text table.

Run twice: once with JSON discovery, once with --format json ignored so the
human-readable table is the only source.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-kiro-cli")
os.environ["BRIDGE_API_KEY"] = "sk-test-123"
os.environ.setdefault("KIRO_DEFAULT_MODEL", "auto")

TEXT_ONLY = os.environ.get("TEXT_ONLY") == "1"
if TEXT_ONLY:
    os.environ["FAKE_NO_JSON_FORMAT"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from kiro_openai.backend import _parse_model_table  # noqa: E402
from kiro_openai.server import app  # noqa: E402

AUTH = {"Authorization": "Bearer sk-test-123"}
failures = []


def check(label, condition, detail=""):
    print("[{0}] {1}{2}".format("PASS" if condition else "FAIL", label,
                                "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


print("== table parser, against the real observed layout ==")
REAL = """Available models (* = default):

* auto                 1.00x credits      Models chosen by task for optimal usage and consistent quality
  claude-opus-5        2.20x credits      Experimental preview of Claude Opus 5 model with 1M context window
  claude-sonnet-5      1.30x credits      Claude Sonnet 5 model with 1M context window
  claude-sonnet-4.6    1.30x credits      Claude Sonnet 4.6 model with 1M context window
  gpt-5.6-luna         0.60x credits      Experimental preview of OpenAI GPT 5.6 Luna
  deepseek-3.2         0.25x credits      Experimental preview of DeepSeek V3.2
  qwen3-coder-next     0.05x credits      Experimental preview of Qwen3 Coder Next
"""
parsed = _parse_model_table(REAL)
print(parsed)
check("all 7 rows parsed", len(parsed) == 7, parsed)
check("starred default keeps its id", parsed[0] == "auto", parsed[:1])
check("dotted versions intact", "claude-sonnet-4.6" in parsed, parsed)
check("sonnet 5 found", "claude-sonnet-5" in parsed, parsed)
check("header line ignored", "Available" not in " ".join(parsed), parsed)
check("prose not mistaken for ids", "Models" not in parsed, parsed)

with TestClient(app) as client:
    health = client.get("/healthz").json()
    print("\n== discovery ({0}) ==".format("text table only" if TEXT_ONLY else "json"))
    print(health["models"])
    check("more than the fallback discovered", len(health["models"]) > 1, health["models"])
    check("claude-sonnet-5 discovered", "claude-sonnet-5" in health["models"], health["models"])

    print("\n== the requested model is the one used ==")
    for wanted in ("claude-sonnet-5", "gpt-5.6-sol", "qwen3-coder-next"):
        r = client.post("/v1/chat/completions", headers=AUTH, json={
            "model": wanted, "messages": [{"role": "user", "content": "hi"}]})
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        check("{0}: passed to the CLI".format(wanted),
              "MODEL={0} ".format(wanted) in content, content.split("\n")[0])
        check("{0}: echoed in the response".format(wanted), body["model"] == wanted, body["model"])
        check("{0}: reported in X-Kiro-Model".format(wanted),
              r.headers.get("X-Kiro-Model") == wanted, dict(r.headers))

    print("\n== no silent substitution ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    check("unknown model -> 404", r.status_code == 404, r.status_code)
    check("error names the model", "gpt-4o" in r.json()["error"]["message"], r.json())
    check("error lists what is available",
          "claude-sonnet-5" in r.json()["error"]["message"], r.json())

    print("\n== omitted model falls back to the default ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "messages": [{"role": "user", "content": "hi"}]})
    check("no model -> default used", r.json()["model"] == "auto", r.json()["model"])

    print("\n== provider-prefixed id ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "kiro/claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]})
    check("prefix stripped and honoured", r.json()["model"] == "claude-sonnet-5", r.json())

    print("\n== streaming honours the model too ==")
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "claude-sonnet-5", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]}) as s:
        check("stream reports the model", s.headers.get("X-Kiro-Model") == "claude-sonnet-5",
              dict(s.headers))
        raw = "".join(s.iter_text())
    check("streamed body used the model", "MODEL=claude-sonnet-5 " in raw, raw[:200])

print("\n" + "=" * 46)
if failures:
    print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("MODEL SELECTION CONFIRMED ({0})".format("text table" if TEXT_ONLY else "json"))
