#!/usr/bin/env python3
"""Behaviour on a CLI build that cannot select a model per request.

The bridge must not pretend the requested model was used.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-kiro-cli")
os.environ["BRIDGE_API_KEY"] = "sk-test-123"
os.environ["FAKE_NO_MODEL_FLAG"] = "1"   # stub drops --model from its help

from fastapi.testclient import TestClient  # noqa: E402

from kiro_openai.server import app  # noqa: E402

AUTH = {"Authorization": "Bearer sk-test-123"}
failures = []


def check(label, condition, detail=""):
    print("[{0}] {1}{2}".format("PASS" if condition else "FAIL", label,
                                "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


with TestClient(app) as client:
    health = client.get("/healthz").json()
    check("model_selection reported false", health["model_selection"] is False, health)

    # Naming a non-default model must fail loudly rather than be served by a
    # different model behind the client's back.
    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
    })
    check("non-default model rejected, not substituted", resp.status_code == 404, resp.status_code)
    message = resp.json()["error"]["message"]
    check("error explains the missing --model flag", "no 'chat --model' flag" in message, message)
    check("error suggests a remedy", "KIRO_DEFAULT_MODEL" in message, message)

    # The configured default still works, since it needs no flag.
    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    content = resp.json()["choices"][0]["message"]["content"]
    check("default model still served", resp.status_code == 200, resp.status_code)
    check("--model never passed to the CLI", "MODEL=None" in content, content)

    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "messages": [{"role": "user", "content": "hi"}]})
    check("omitted model still works", resp.status_code == 200, resp.status_code)

print("\n" + "=" * 46)
if failures:
    print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("DEGRADED-CLI BEHAVIOUR CONFIRMED")
