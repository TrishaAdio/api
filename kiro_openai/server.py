from __future__ import annotations

import json
import os
import time
import uuid
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import usage, webapp
from .backend import KiroError, ModelNotAvailable, backend
from .config import settings
from .prompt import build_prompt, estimate_tokens
from .schemas import ChatCompletionRequest, Model, ModelList

app = FastAPI(title="kiro-openai-bridge", version="0.2.0")


# ---------------------------------------------------------------------- helpers


def _openai_error(status: int, message: str, err_type: str = "invalid_request_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


async def require_auth(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Optional[int]:
    """Authenticate a generated API key. Valid from any IP.

    Keys are issued in the console and stored hashed. BRIDGE_API_KEY from .env
    is also accepted so the bridge is usable before any key is minted.
    Returns the key's row id when one matched, for per-key usage attribution.
    """
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    elif authorization:
        supplied = authorization.strip()

    if not supplied:
        raise HTTPException(status_code=401, detail="Missing API key. Send it as a bearer token.")

    if settings.bridge_api_key and supplied == settings.bridge_api_key:
        return None

    record = await usage.verify_key(supplied)
    if record is None:
        raise HTTPException(
            status_code=401,
            detail="Incorrect API key provided. Generate one in the RioApis console.",
        )

    await usage.touch_key(record["id"], webapp.client_ip(request))
    request.state.api_key_id = record["id"]
    return record["id"]


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    err_type = "authentication_error" if exc.status_code == 401 else "invalid_request_error"
    return _openai_error(exc.status_code, str(exc.detail), err_type)


@app.on_event("startup")
async def _startup() -> None:
    await usage.init()
    try:
        await backend.startup()
    except KiroError as exc:
        # Do not crash the process: surface the problem per-request instead, so
        # a misconfigured CLI is debuggable via /healthz.
        app.state.startup_error = str(exc)
    else:
        app.state.startup_error = None


app.include_router(webapp.router)

if settings.enable_web_ui and os.path.isdir(webapp.STATIC_DIR):
    app.mount("/static", StaticFiles(directory=webapp.STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def web_index():
        return await webapp.index()


# ----------------------------------------------------------------------- routes


@app.get("/healthz")
async def healthz():
    error = getattr(app.state, "startup_error", None)
    return {
        "status": "ok" if backend.ready else "degraded",
        "cli": settings.cli_bin,
        "backend": settings.backend,
        "streaming": "live" if settings.use_acp else "replayed",
        "models": backend.models(),
        "model_selection": backend.supports_model_flag,
        "default_model": settings.default_model,
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


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    key_id: Optional[int] = Depends(require_auth),
):
    startup_error = getattr(app.state, "startup_error", None)
    if startup_error:
        return _openai_error(503, startup_error, "server_error")

    if not body.messages:
        return _openai_error(400, "'messages' must contain at least one message")

    prompt = build_prompt(body.messages)
    if not prompt.strip():
        return _openai_error(400, "'messages' contained no usable text content")

    ip = webapp.client_ip(request)
    agent = request.headers.get("user-agent", "")

    # A model named in the request is honoured exactly or rejected outright.
    try:
        model = backend.resolve_model(body.model)
    except ModelNotAvailable as exc:
        await usage.record(ip=ip, model=body.model or "", status=404,
                           user_agent=agent, error=str(exc), key_id=key_id)
        return _openai_error(404, str(exc), "invalid_request_error")

    completion_id = "chatcmpl-{0}".format(uuid.uuid4().hex)
    created = int(time.time())
    headers = {"X-Kiro-Model": model}
    started = time.perf_counter()

    if body.stream:
        stream_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        stream_headers.update(headers)
        return StreamingResponse(
            _stream(completion_id, created, model, prompt, body.reasoning_effort, ip, agent, key_id),
            media_type="text/event-stream",
            headers=stream_headers,
        )

    try:
        collected = []
        async for kind, piece in backend.stream_reply(
            prompt, model=model, effort=body.reasoning_effort
        ):
            if kind == "text":
                collected.append(piece)
        text = "".join(collected)
    except KiroError as exc:
        await usage.record(ip=ip, model=model, status=502,
                           latency_ms=int((time.perf_counter() - started) * 1000),
                           user_agent=agent, error=str(exc), key_id=key_id)
        return _openai_error(502, str(exc), "server_error")

    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(text)
    await usage.record(
        ip=ip,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_multiplier=backend.model_cost(model),
        latency_ms=int((time.perf_counter() - started) * 1000),
        status=200,
        user_agent=agent,
        key_id=key_id,
    )

    return JSONResponse(
        headers=headers,
        content={
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
        },
    )


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
    ip: str = "",
    user_agent: str = "",
    key_id: Optional[int] = None,
) -> AsyncIterator[str]:
    """Emit an OpenAI SSE stream.

    `--no-interactive` returns the whole answer at once, so this is chunked
    replay rather than incremental generation: clients that require the stream
    protocol work correctly, but the first token arrives only once the full
    response is ready. Switch to the ACP backend for genuine token streaming.
    """
    started = time.perf_counter()
    yield _chunk(completion_id, created, model, {"role": "assistant", "content": ""}, None)

    parts = []
    try:
        # Forwarded as they arrive, so this is real streaming when the ACP
        # backend is active. Reasoning chunks are dropped: OpenAI's schema has
        # no field for them.
        async for kind, piece in backend.stream_reply(prompt, model=model, effort=effort):
            if kind != "text" or not piece:
                continue
            parts.append(piece)
            # ACP chunks are already fine-grained; the CLI backend hands over
            # the whole answer at once, so slice to keep deltas incremental
            # for clients that render as they read.
            size = max(1, settings.stream_chunk_size)
            for at in range(0, len(piece), size):
                yield _chunk(completion_id, created, model,
                             {"content": piece[at : at + size]}, None)
    except KiroError as exc:
        await usage.record(ip=ip, model=model, status=502, stream=True,
                           latency_ms=int((time.perf_counter() - started) * 1000),
                           user_agent=user_agent, error=str(exc), key_id=key_id)
        yield "data: {0}\n\n".format(
            json.dumps({"error": {"message": str(exc), "type": "server_error"}})
        )
        yield "data: [DONE]\n\n"
        return

    text = "".join(parts)

    await usage.record(
        ip=ip,
        model=model,
        prompt_tokens=estimate_tokens(prompt),
        completion_tokens=estimate_tokens(text),
        cost_multiplier=backend.model_cost(model),
        latency_ms=int((time.perf_counter() - started) * 1000),
        status=200,
        stream=True,
        user_agent=user_agent,
        key_id=key_id,
    )

    yield _chunk(completion_id, created, model, {}, "stop")
    yield "data: [DONE]\n\n"
