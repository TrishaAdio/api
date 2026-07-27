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

    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "claude-sonnet-4.5",
        "messages": [{"role": "user", "content": "hi"}],
    })
    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    check("warning header explains the limitation",
          "cannot select a model" in resp.headers.get("X-Kiro-Warning", ""),
          dict(resp.headers))
    check("response does NOT echo the unhonoured model",
          data["model"] != "claude-sonnet-4.5", data["model"])
    check("response echoes the effective default", data["model"] == "auto", data["model"])
    check("--model never passed to the CLI", "MODEL=None" in content, content)
    check("request still succeeds", resp.status_code == 200)

print("\n" + "=" * 46)
if failures:
    print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("DEGRADED-CLI BEHAVIOUR CONFIRMED")
