"""RioApis web console: chat, API keys, IP whitelist, usage and docs.

Two distinct trust boundaries:

  * The console and everything under /api is restricted by **caller IP**. The
    owner's IP is seeded into .env by setup.sh and cannot be removed from the
    web; further IPs are added at runtime and stored in the database.
  * The OpenAI-compatible /v1 surface is authenticated by **generated API
    keys**, which work from any IP.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import usage
from .backend import KiroError, ModelNotAvailable, backend, split_thinking
from .config import settings
from .prompt import build_prompt, estimate_tokens
from .schemas import ChatMessage

router = APIRouter(prefix="/api")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ─── helpers ──────────────────────────────────────────────────────────────


def client_ip(request: Request) -> str:
    """Best-effort caller IP, honouring proxy headers when configured."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else ""


async def is_admin_ip(ip: str) -> bool:
    if not ip:
        return False
    if ip in settings.admin_ip_set():
        return True
    return any(row["ip"] == ip for row in await usage.list_whitelist())


async def require_admin(request: Request) -> str:
    """Gate the console on the caller's IP and return it."""
    ip = client_ip(request)
    if not await is_admin_ip(ip):
        raise HTTPException(
            status_code=403,
            detail="Address {0} is not whitelisted for the RioApis console.".format(ip or "unknown"),
        )
    return ip


def usd(credits: float) -> float:
    return round(float(credits or 0) * settings.usd_per_credit, 4)


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


# ─── payloads ─────────────────────────────────────────────────────────────


class WebChatRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    effort: Optional[str] = None


class SettingsPatch(BaseModel):
    kiro_api_key: Optional[str] = None
    default_model: Optional[str] = None
    trust_tools: Optional[str] = None
    usd_per_credit: Optional[float] = None
    plan_name: Optional[str] = None
    plan_credits: Optional[float] = None
    show_thinking: Optional[bool] = None


class KeyCreate(BaseModel):
    name: str = ""


class WhitelistAdd(BaseModel):
    ip: str
    label: str = ""


# ─── session ──────────────────────────────────────────────────────────────


@router.get("/session")
async def session_probe(request: Request):
    """Unauthenticated: lets the UI show the caller their own IP when denied."""
    ip = client_ip(request)
    return {"ip": ip, "admin": await is_admin_ip(ip), "brand": settings.brand}


@router.get("/bootstrap")
async def bootstrap(_ip: str = Depends(require_admin)):
    return {
        "brand": settings.brand,
        "models": backend.model_catalog(),
        "default_model": settings.default_model,
        "model_selection": backend.supports_model_flag,
        "show_thinking": settings.show_thinking,
        "ready": backend.ready,
        "cli": settings.cli_bin,
        "port": settings.port,
        "usd_per_credit": settings.usd_per_credit,
    }


# ─── usage ────────────────────────────────────────────────────────────────


@router.get("/usage")
async def usage_log(
    limit: int = 100,
    model: Optional[str] = None,
    ip: Optional[str] = None,
    _ip: str = Depends(require_admin),
):
    rows = await usage.recent(limit=limit, model=model, ip=ip)
    for row in rows:
        row["usd"] = usd(row.get("credits", 0))
    return {"entries": rows, "usd_per_credit": settings.usd_per_credit}


@router.get("/stats")
async def usage_stats(_ip: str = Depends(require_admin)):
    data = await usage.stats()
    data["totals"]["usd"] = usd(data["totals"].get("credits", 0))
    data["last_24h"]["usd"] = usd(data["last_24h"].get("credits", 0))
    data["period"]["usd"] = usd(data["period"].get("credits", 0))
    data["budget"] = _budget(data["period"])
    for row in data["by_model"]:
        row["usd"] = usd(row.get("credits", 0))
    for row in data["by_ip"]:
        row["usd"] = usd(row.get("credits", 0))
    for point in data["series"]:
        point["usd"] = usd(point.get("credits", 0))
    data["usd_per_credit"] = settings.usd_per_credit
    return data


@router.delete("/usage")
async def clear_usage(_ip: str = Depends(require_admin)):
    return {"deleted": await usage.purge()}


# ─── API keys ─────────────────────────────────────────────────────────────


@router.get("/keys")
async def list_keys(_ip: str = Depends(require_admin)):
    keys = await usage.list_keys()
    for key in keys:
        key["usd"] = usd(key.get("credits", 0))
    return {"keys": keys}


@router.post("/keys")
async def create_key(body: KeyCreate, ip: str = Depends(require_admin)):
    """Returns the plaintext key exactly once; only its hash is stored."""
    created = await usage.create_key(name=body.name.strip()[:60], created_by=ip)
    return created


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: int, _ip: str = Depends(require_admin)):
    if not await usage.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="No such key")
    return {"revoked": key_id}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, _ip: str = Depends(require_admin)):
    if not await usage.delete_key(key_id):
        raise HTTPException(status_code=404, detail="No such key")
    return {"deleted": key_id}


# ─── whitelist ────────────────────────────────────────────────────────────


@router.get("/whitelist")
async def get_whitelist(_ip: str = Depends(require_admin)):
    rows = await usage.list_whitelist()
    roots = [
        {"ip": value, "label": "owner (.env)", "root": True}
        for value in sorted(settings.admin_ip_set())
        if value not in ("testclient",)
    ]
    for row in rows:
        row["root"] = False
    return {"entries": roots + rows}


@router.post("/whitelist")
async def post_whitelist(body: WhitelistAdd, ip: str = Depends(require_admin)):
    candidate = body.ip.strip()
    if not _valid_ip(candidate):
        raise HTTPException(status_code=400, detail="'{0}' is not a valid IP address".format(candidate))
    if not await usage.add_whitelist(candidate, body.label.strip()[:60], ip):
        raise HTTPException(status_code=409, detail="{0} is already whitelisted".format(candidate))
    return {"added": candidate}


@router.delete("/whitelist/{ip_value}")
async def delete_whitelist(ip_value: str, _ip: str = Depends(require_admin)):
    # Owner IPs live in .env precisely so a mistake in the UI cannot lock
    # everyone out of the console.
    if ip_value in settings.admin_ip_set():
        raise HTTPException(
            status_code=400,
            detail="{0} is an owner address from .env and cannot be removed here.".format(ip_value),
        )
    if not await usage.remove_whitelist(ip_value):
        raise HTTPException(status_code=404, detail="Not whitelisted")
    return {"removed": ip_value}


# ─── settings ─────────────────────────────────────────────────────────────


@router.get("/settings")
async def read_settings(_ip: str = Depends(require_admin)):
    """Never returns the raw Kiro key, only whether one is set and its shape."""
    key = os.getenv("KIRO_API_KEY", "")
    return {
        "kiro_api_key_set": bool(key),
        "kiro_api_key_masked": _mask(key),
        "default_model": settings.default_model,
        "trust_tools": ",".join(settings.trust_tools),
        "usd_per_credit": settings.usd_per_credit,
        "plan_name": settings.plan_name,
        "plan_credits": settings.plan_credits,
        "show_thinking": settings.show_thinking,
        "cli": settings.cli_bin,
        "model_selection": backend.supports_model_flag,
        "env_file": settings.env_file,
    }


@router.post("/settings")
async def write_settings(patch: SettingsPatch, _ip: str = Depends(require_admin)):
    updates: Dict[str, str] = {}

    if patch.kiro_api_key is not None:
        key = patch.kiro_api_key.strip()
        if key and not key.startswith("ksk_"):
            return JSONResponse(status_code=400, content={"error": "Kiro API keys start with 'ksk_'."})
        if key:
            updates["KIRO_API_KEY"] = key
            os.environ["KIRO_API_KEY"] = key

    if patch.default_model is not None:
        model = patch.default_model.strip()
        if model and model not in backend.models():
            return JSONResponse(status_code=400, content={"error": "Unknown model '{0}'.".format(model)})
        if model:
            updates["KIRO_DEFAULT_MODEL"] = model
            settings.default_model = model

    if patch.trust_tools is not None:
        tools = patch.trust_tools.strip()
        updates["KIRO_TRUST_TOOLS"] = tools
        settings.trust_tools = [t.strip() for t in tools.split(",") if t.strip()]

    if patch.usd_per_credit is not None:
        rate = float(patch.usd_per_credit)
        if rate < 0:
            return JSONResponse(status_code=400, content={"error": "Rate must not be negative."})
        updates["USD_PER_CREDIT"] = str(rate)
        settings.usd_per_credit = rate

    if patch.plan_credits is not None:
        allowance = float(patch.plan_credits)
        if allowance < 0:
            return JSONResponse(status_code=400, content={"error": "Allowance must not be negative."})
        updates["PLAN_CREDITS"] = str(allowance)
        settings.plan_credits = allowance

    if patch.plan_name is not None:
        name = patch.plan_name.strip()[:40]
        updates["PLAN_NAME"] = name
        settings.plan_name = name

    if patch.show_thinking is not None:
        settings.show_thinking = bool(patch.show_thinking)
        updates["KIRO_SHOW_THINKING"] = "true" if settings.show_thinking else "false"
        # Push it to the CLI too, or it keeps emitting (or withholding) reasoning.
        await backend.apply_thinking_setting()

    if not updates:
        return {"saved": []}

    try:
        await asyncio.get_event_loop().run_in_executor(None, _write_env, updates)
    except OSError as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})

    # The CLI subprocess inherits our environment, which was just updated, so
    # changes apply to the next request without a restart.
    return {"saved": sorted(updates.keys())}


# ─── chat ─────────────────────────────────────────────────────────────────


@router.post("/chat")
async def web_chat(body: WebChatRequest, request: Request, ip: str = Depends(require_admin)):
    """Streaming chat for the console. Emits newline-delimited JSON events."""
    if not body.messages:
        raise HTTPException(status_code=400, detail="No messages")

    prompt = build_prompt(body.messages)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="No usable message content")

    agent = request.headers.get("user-agent", "")

    try:
        model = backend.resolve_model(body.model)
    except ModelNotAvailable as exc:
        await usage.record(ip=ip, model=body.model or "", status=404, source="web",
                           user_agent=agent, error=str(exc))
        raise HTTPException(status_code=404, detail=str(exc))

    async def events():
        started = time.perf_counter()
        yield _event({"type": "start", "model": model})

        chunks: List[str] = []
        try:
            # Chunks are forwarded the moment they arrive, so reasoning appears
            # in the console while the model is still working.
            async for kind, piece in backend.stream_reply(
                prompt, model=model, effort=body.effort
            ):
                if kind == "thought":
                    yield _event({"type": "thinking_delta", "text": piece})
                else:
                    chunks.append(piece)
                    # The CLI backend yields the answer in one piece; slicing
                    # keeps the console rendering progressively either way.
                    step = max(1, settings.stream_chunk_size)
                    for at in range(0, len(piece), step):
                        yield _event({"type": "delta", "text": piece[at : at + step]})
                        await asyncio.sleep(0)
        except KiroError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            await usage.record(ip=ip, model=model, latency_ms=elapsed, status=502,
                               stream=True, source="web", user_agent=agent, error=str(exc))
            yield _event({"type": "error", "message": str(exc)})
            return

        elapsed = int((time.perf_counter() - started) * 1000)
        text = "".join(chunks)

        prompt_tokens = estimate_tokens(prompt)
        completion_tokens = estimate_tokens(text)
        multiplier = backend.model_cost(model)
        await usage.record(
            ip=ip, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_multiplier=multiplier, latency_ms=elapsed, status=200,
            stream=True, source="web", user_agent=agent,
        )

        yield _event({
            "type": "done",
            "model": model,
            "latency_ms": elapsed,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "usd": usd(multiplier),
        })

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── internals ────────────────────────────────────────────────────────────


def _budget(period: Dict[str, Any]) -> Dict[str, Any]:
    """What is left of the monthly allowance, in credits and dollars."""
    allowance = max(0.0, float(settings.plan_credits))
    used = max(0.0, float(period.get("credits", 0)))
    remaining = max(0.0, allowance - used)
    over = max(0.0, used - allowance)

    return {
        "plan": settings.plan_name,
        "allowance_credits": round(allowance, 4),
        "allowance_usd": usd(allowance),
        "used_credits": round(used, 4),
        "used_usd": usd(used),
        "remaining_credits": round(remaining, 4),
        "remaining_usd": usd(remaining),
        # Anything past the allowance is billed as pay-as-you-go overage.
        "over_credits": round(over, 4),
        "over_usd": usd(over),
        "percent_used": round(min(100.0, (used / allowance) * 100), 2) if allowance else 0.0,
        "requests": period.get("requests", 0),
        "period_label": period.get("label", ""),
        "reset": period.get("reset"),
    }


def _event(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 10:
        return "****"
    return "{0}...{1}".format(secret[:8], secret[-4:])


_ENV_LINE_RE = re.compile(r"^([A-Z0-9_]+)=")


def _write_env(updates: Dict[str, str]) -> None:
    """Update keys in .env in place, preserving comments and ordering."""
    path = settings.env_file
    lines: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    remaining = dict(updates)
    for index, line in enumerate(lines):
        match = _ENV_LINE_RE.match(line.strip())
        if not match:
            continue
        key = match.group(1)
        if key in remaining:
            lines[index] = "{0}={1}".format(key, remaining.pop(key))

    for key, value in remaining.items():
        lines.append("{0}={1}".format(key, value))

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip("\n") + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


async def index() -> HTMLResponse:
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())
