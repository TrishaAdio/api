#!/usr/bin/env python3
"""End-to-end check of the OpenAI wire contract, driven against the stub CLI."""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["KIRO_CLI_BIN"] = str(ROOT / "tools" / "fake-kiro-cli")
os.environ["BRIDGE_API_KEY"] = "sk-test-123"
os.environ["KIRO_TRUST_TOOLS"] = "read,grep"
os.environ["KIRO_ARGV_PROMPT_LIMIT"] = "400"

from fastapi.testclient import TestClient  # noqa: E402

from kiro_openai.server import app  # noqa: E402

AUTH = {"Authorization": "Bearer sk-test-123"}
failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print("[{0}] {1}{2}".format(status, label, "" if condition else "  <- " + str(detail)))
    if not condition:
        failures.append(label)


with TestClient(app) as client:
    health = client.get("/healthz").json()
    print("\n== /healthz ==")
    print(json.dumps(health, indent=2))
    check("startup succeeded", health["error"] is None, health["error"])
    check("models discovered from --list-models", "claude-sonnet-4.6" in health["models"], health["models"])

    print("\n== auth ==")
    check("missing key -> 401", client.post("/v1/chat/completions", json={"messages": []}).status_code == 401)
    body = client.post("/v1/chat/completions", json={"messages": []}).json()
    check("401 uses OpenAI error envelope", "error" in body and "message" in body["error"], body)

    print("\n== /v1/models ==")
    models = client.get("/v1/models", headers=AUTH).json()
    check("object == list", models["object"] == "list", models)
    check("entries shaped like OpenAI models",
          all(m["object"] == "model" and "id" in m for m in models["data"]), models)
    check("single-model retrieve works",
          client.get("/v1/models/gpt-5.6-sol", headers=AUTH).status_code == 200)
    check("unknown model -> 404",
          client.get("/v1/models/nope", headers=AUTH).status_code == 404)

    print("\n== non-streaming completion ==")
    resp = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hello"}],
    })
    check("HTTP 200", resp.status_code == 200, resp.text)
    data = resp.json()
    print(json.dumps(data, indent=2))
    check("object == chat.completion", data["object"] == "chat.completion")
    check("id prefix", data["id"].startswith("chatcmpl-"))
    check("assistant role", data["choices"][0]["message"]["role"] == "assistant")
    check("finish_reason stop", data["choices"][0]["finish_reason"] == "stop")
    check("usage present", set(data["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"})
    content = data["choices"][0]["message"]["content"]
    check("ANSI + spinner stripped", "\x1b" not in content and "⠋" not in content, repr(content[:60]))
    check("model flag forwarded", "MODEL=gpt-5.6-sol" in content, content)
    check("wrap never forwarded", "WRAP=never" in content, content)
    check("trust-tools forwarded", "TRUST=read,grep" in content, content)
    check("single turn sent verbatim", "SAW_PLAIN" in content, content)

    print("\n== model id normalisation ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "kiro/claude-sonnet-4.6", "messages": [{"role": "user", "content": "hi"}]})
    check("provider-prefixed id resolved", r.json()["model"] == "claude-sonnet-4.6", r.json()["model"])
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    check("unknown id rejected, never substituted", r.status_code == 404, r.status_code)

    print("\n== system prompt + multi-turn + parts content ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "auto",
        "reasoning_effort": "high",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": [{"type": "text", "text": "second"},
                                         {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]},
        ],
    })
    content = r.json()["choices"][0]["message"]["content"]
    print(content)
    check("system -> instructions block", "INSTRUCTIONS=yes" in content, content)
    check("multi-turn -> transcript", "SAW_TRANSCRIPT" in content, content)
    check("reasoning_effort -> --effort", "EFFORT=high" in content, content)

    print("\n== long prompt goes to stdin ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "messages": [{"role": "user", "content": "x" * 3000}]})
    content = r.json()["choices"][0]["message"]["content"]
    print(content)
    argv_len = int(content.split("ARGV_PROMPT_LEN=")[1].split("\n")[0])
    stdin_len = int(content.split("STDIN_LEN=")[1].split("\n")[0])
    check("argv stayed short", argv_len < 400, argv_len)
    check("payload moved to stdin", stdin_len > 2900, stdin_len)

    print("\n== streaming (SSE) ==")
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "auto", "stream": True,
        "messages": [{"role": "user", "content": "stream me"}],
    }) as stream:
        check("stream content-type", "text/event-stream" in stream.headers["content-type"],
              stream.headers.get("content-type"))
        raw = "".join(chunk for chunk in stream.iter_text())

    events = [line[len("data: "):] for line in raw.splitlines() if line.startswith("data: ")]
    check("terminates with [DONE]", events[-1] == "[DONE]", events[-1] if events else None)
    parsed = [json.loads(e) for e in events if e != "[DONE]"]
    check("all chunks typed chat.completion.chunk",
          all(p["object"] == "chat.completion.chunk" for p in parsed))
    check("first chunk opens with role", parsed[0]["choices"][0]["delta"].get("role") == "assistant",
          parsed[0])
    check("last chunk carries finish_reason",
          parsed[-1]["choices"][0]["finish_reason"] == "stop", parsed[-1])
    check("multiple content deltas", len(parsed) > 3, len(parsed))
    joined = "".join(p["choices"][0]["delta"].get("content", "") for p in parsed)
    check("reassembled stream is non-empty", "SAW_PLAIN" in joined, joined[:120])
    check("stream ids stable", len({p["id"] for p in parsed}) == 1)

    print("\n== CLI '> ' answer marker ==")
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "auto", "messages": [{"role": "user", "content": "marker"}]})
    content = r.json()["choices"][0]["message"]["content"]
    check("leading '> ' stripped", not content.startswith(">"), repr(content[:30]))
    check("body preserved after stripping", content.startswith("MODEL="), repr(content[:30]))

    print("\n== model selection is reported honestly ==")
    check("healthz advertises model_selection",
          client.get("/healthz").json()["model_selection"] is True)
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    check("unknown model -> 404 not substitution", r.status_code == 404, r.status_code)
    r = client.post("/v1/chat/completions", headers=AUTH, json={
        "model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]})
    check("honoured model reported in X-Kiro-Model",
          r.headers.get("X-Kiro-Model") == "gpt-5.6-sol", dict(r.headers))

    print("\n== error propagation ==")
    check("empty messages -> 400",
          client.post("/v1/chat/completions", headers=AUTH, json={"messages": []}).status_code == 400)
    check("whitespace-only content -> 400",
          client.post("/v1/chat/completions", headers=AUTH,
                      json={"messages": [{"role": "user", "content": "   "}]}).status_code == 400)

print("\n" + "=" * 46)
if failures:
    print("FAILED ({0}): {1}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL CHECKS PASSED")
