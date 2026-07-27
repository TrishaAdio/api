from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .backend import KiroError, backend
from .config import settings
from .prompt import build_prompt, estimate_tokens
from .schemas import ChatCompletionRequest, Model, ModelList

app = FastAPI(title="kiro-openai-bridge", version="0.1.0")


# ---------------------------------------------------------------------- helpers


def _openai_error(status: int, message: str, err_type: str = "invalid_request_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


async def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """Enforce the bridge's own bearer token, mirroring OpenAI's 401 shape."""
    if not settings.bridge_api_key:
        return
    expected = "Bearer {0}".format(settings.bridge_api_key)
    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Incorrect API key provided. Set BRIDGE_API_KEY and send it as a bearer token.",
        )


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    err_type = "authentication_error" if exc.status_code == 401 else "invalid_request_error"
    return _openai_error(exc.status_code, str(exc.detail), err_type)


@app.on_event("startup")
async def _startup() -> None:
    try:
        await backend.startup()
    except KiroError as exc:
        # Do not crash the process: surface the problem per-request instead, so
        # a misconfigured CLI is debuggable via /healthz.
        app.state.startup_error = str(exc)
    else:
        app.state.startup_error = None


# ----------------------------------------------------------------------- routes


@app.get("/healthz")
async def healthz():
    error = getattr(app.state, "startup_error", None)
    return {
        "status": "ok" if backend.ready else "degraded",
        "cli": settings.cli_bin,
        "models": backend.models(),
        "trust_tools": settings.trust_tools,
        "error": error,
    }


@app.get("/v1/models", dependencies=[Depends(require_auth)])
async def list_models() -> ModelList:
    created = int(time.time())
    return ModelList(data=[Model(id=m, created=created) for m in backend.models()])


@app.get("/v1/models/{model_id:path}", dependencies=[Depends(require_auth)])
async def retrieve_model(model_id: str):
    if model_id not in backend.models():
        raise HTTPException(status_code=404, detail="The model '{0}' does not exist".format(model_id))
    return Model(id=model_id, created=int(time.time()))


@app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
async def chat_completions(body: ChatCompletionRequest):
    startup_error = getattr(app.state, "startup_error", None)
    if startup_error:
        return _openai_error(503, startup_error, "server_error")

    if not body.messages:
        return _openai_error(400, "'messages' must contain at least one message")

    prompt = build_prompt(body.messages)
    if not prompt.strip():
        return _openai_error(400, "'messages' contained no usable text content")

    model = backend.resolve_model(body.model)
    completion_id = "chatcmpl-{0}".format(uuid.uuid4().hex)
    created = int(time.time())

    if body.stream:
        return StreamingResponse(
            _stream(completion_id, created, model, prompt, body.reasoning_effort),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        text = await backend.complete(prompt, model=model, effort=body.reasoning_effort)
    except KiroError as exc:
        return _openai_error(502, str(exc), "server_error")

    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(text)

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        # Estimated: the headless CLI does not report token counts.
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# --------------------------------------------------------------------- streaming


def _chunk(completion_id: str, created: int, model: str, delta: dict, finish: Optional[str]) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
    }
    return "data: {0}\n\n".format(json.dumps(payload, ensure_ascii=False))


async def _stream(
    completion_id: str,
    created: int,
    model: str,
    prompt: str,
    effort: Optional[str],
) -> AsyncIterator[str]:
    """Emit an OpenAI SSE stream.

    `--no-interactive` returns the whole answer at once, so this is chunked
    replay rather than incremental generation: clients that require the stream
    protocol work correctly, but the first token arrives only once the full
    response is ready. Switch to the ACP backend for genuine token streaming.
    """
    try:
        text = await backend.complete(prompt, model=model, effort=effort)
    except KiroError as exc:
        yield "data: {0}\n\n".format(
            json.dumps({"error": {"message": str(exc), "type": "server_error"}})
        )
        yield "data: [DONE]\n\n"
        return

    yield _chunk(completion_id, created, model, {"role": "assistant", "content": ""}, None)

    size = max(1, settings.stream_chunk_size)
    for index in range(0, len(text), size):
        yield _chunk(completion_id, created, model, {"content": text[index : index + size]}, None)
        await asyncio.sleep(0)

    yield _chunk(completion_id, created, model, {}, "stop")
    yield "data: [DONE]\n\n"
