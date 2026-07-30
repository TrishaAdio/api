"""ACP (Agent Client Protocol) backend — JSON-RPC 2.0 over stdio.

Why this exists: `kiro-cli chat --no-interactive` renders markdown for a
terminal, so fenced code arrives as a language label plus plain lines and the
original syntax is gone. ACP delivers the model's raw markdown instead, and
streams reasoning separately, which is what the console needs.

The wire format is pinned down in two places that disagree:

  * kiro.dev/docs/cli/acp lists `session/notification` with PascalCase update
    names (AgentMessageChunk, TurnEnd) and names the prompt array `content`.
  * The ACP spec it defers to uses `session/update` with a snake_case
    `sessionUpdate` discriminator, names the array `prompt`, and ends a turn
    with `stopReason` on the prompt response.

Rather than bet on either, this client accepts both dialects: notification
method and discriminator names are normalised, and the prompt parameter is
retried under the other name if the first is rejected.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from .config import settings

# Normalised update kinds we act on.
_MESSAGE = "agentmessagechunk"
_THOUGHT = "agentthoughtchunk"
_TURN_END = "turnend"

_STOP_OK = {"end_turn", "endturn", "max_tokens", "maxtokens"}


class AcpError(RuntimeError):
    """Raised when the ACP agent cannot be started or fails a request."""


def _norm(name: Optional[str]) -> str:
    """AgentMessageChunk / agent_message_chunk -> agentmessagechunk."""
    if not name:
        return ""
    return name.replace("_", "").replace("-", "").replace("/", "").lower()


def _blocks_to_text(value: Any) -> str:
    """Pull text out of a ContentBlock, a list of them, or a bare string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        # Some shapes nest the block under "content".
        if "content" in value:
            return _blocks_to_text(value["content"])
        return ""
    if isinstance(value, list):
        return "".join(_blocks_to_text(item) for item in value)
    return ""


def _update_of(params: Dict[str, Any]) -> Dict[str, Any]:
    update = params.get("update")
    if isinstance(update, dict):
        return update
    # kiro.dev's shape puts the fields directly on params.
    return params


def _kind_of(update: Dict[str, Any]) -> str:
    for key in ("sessionUpdate", "type", "kind", "update"):
        value = update.get(key)
        if isinstance(value, str):
            return _norm(value)
    return ""


class AcpClient:
    """One `kiro-cli acp` process, driven for a single prompt turn.

    A process per turn keeps the mapping to OpenAI's stateless API honest and
    avoids leaking session state between callers.
    """

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._session_id: Optional[str] = None
        self._stderr: List[str] = []

    # ── process ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        argv = [settings.cli_bin, "acp"]
        if settings.agent:
            argv += ["--agent", settings.agent]

        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        env["KIRO_LOG_NO_COLOR"] = "1"

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=settings.workdir if os.path.isdir(settings.workdir) else None,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AcpError("cannot execute {0!r}: {1}".format(settings.cli_bin, exc))

    async def close(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    # ── framing ──────────────────────────────────────────────────────────

    def _send(self, payload: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AcpError("ACP process is not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))

    async def _flush(self) -> None:
        if self._proc and self._proc.stdin:
            await self._proc.stdin.drain()

    def _request(self, method: str, params: Dict[str, Any]) -> int:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    def _respond(self, request_id: Any, result: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _respond_error(self, request_id: Any, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": message}})

    async def _read(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Next JSON message, or None at end of stream."""
        if self._proc is None or self._proc.stdout is None:
            return None
        while True:
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                raise AcpError("ACP agent stopped responding after {0:.0f}s".format(timeout))
            if not raw:
                return None
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # The agent may log to stdout; ignore anything that is not JSON.
                continue

    async def _drain_stderr(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self._proc.stderr.read(4096), timeout=0.4)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ""
        return data.decode("utf-8", "replace").strip()

    # ── agent-initiated requests ─────────────────────────────────────────

    def _answer_agent_request(self, message: Dict[str, Any]) -> None:
        """Reply to anything the agent asks us, so a turn cannot deadlock.

        Permission prompts are declined rather than granted: the bridge runs
        unattended, and granting filesystem or shell access to an HTTP caller
        is not a decision this layer should make silently.
        """
        method = _norm(message.get("method"))
        request_id = message.get("id")

        if "requestpermission" in method:
            options = ((message.get("params") or {}).get("options") or [])
            chosen = None
            for option in options:
                blob = _norm(json.dumps(option))
                if "reject" in blob or "deny" in blob or "no" == _norm(option.get("name")):
                    chosen = option.get("optionId") or option.get("id")
                    break
            if chosen:
                self._respond(request_id, {"outcome": {"outcome": "selected", "optionId": chosen}})
            else:
                self._respond(request_id, {"outcome": {"outcome": "cancelled"}})
            return

        if "readtextfile" in method or "writetextfile" in method or "terminal" in method:
            self._respond_error(request_id, "client capability not offered")
            return

        # Unknown request: answer with an empty result rather than stalling.
        self._respond(request_id, {})

    # ── handshake ────────────────────────────────────────────────────────

    async def handshake(self, timeout: float) -> Dict[str, Any]:
        init_id = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                # Declared false: this bridge intentionally offers no host
                # filesystem or terminal to the agent.
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "rioapis", "version": "0.3.0"},
        })
        await self._flush()

        result = await self._await_result(init_id, timeout)

        session_id = await self._new_session(timeout)
        self._session_id = session_id
        return result

    async def _new_session(self, timeout: float) -> str:
        request_id = self._request("session/new", {
            "cwd": settings.workdir,
            "mcpServers": [],
        })
        await self._flush()
        result = await self._await_result(request_id, timeout)

        for key in ("sessionId", "session_id", "id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        raise AcpError("session/new returned no session id: {0}".format(result))

    async def _await_result(self, request_id: int, timeout: float) -> Dict[str, Any]:
        """Read until the response to request_id arrives, servicing requests."""
        while True:
            message = await self._read(timeout)
            if message is None:
                detail = await self._drain_stderr()
                raise AcpError("ACP agent exited during handshake. {0}".format(detail)[:800])

            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise AcpError("ACP error: {0}".format(message["error"]))
                result = message.get("result")
                return result if isinstance(result, dict) else {}

            if message.get("method") and message.get("id") is not None:
                self._answer_agent_request(message)
                await self._flush()

    # ── model selection ──────────────────────────────────────────────────

    async def set_model(self, model: str, timeout: float) -> bool:
        """Best-effort session/set_model. Its schema is not published."""
        request_id = self._request("session/set_model", {
            "sessionId": self._session_id,
            "modelId": model,
            "model": model,
        })
        await self._flush()
        try:
            await self._await_result(request_id, timeout)
            return True
        except AcpError:
            return False

    # ── prompting ────────────────────────────────────────────────────────

    async def stream(self, text: str, timeout: float) -> AsyncIterator[Tuple[str, str]]:
        """Yield ("thought" | "text", chunk) as the agent produces them."""
        blocks = [{"type": "text", "text": text}]

        # kiro.dev's example uses "content"; the ACP spec uses "prompt".
        # Sending both satisfies either without a second round trip.
        request_id = self._request("session/prompt", {
            "sessionId": self._session_id,
            "prompt": blocks,
            "content": blocks,
        })
        await self._flush()

        while True:
            message = await self._read(timeout)
            if message is None:
                detail = await self._drain_stderr()
                if detail:
                    raise AcpError("ACP agent exited: {0}".format(detail[:800]))
                return

            # Response to our prompt: the turn is over.
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise AcpError("ACP error: {0}".format(message["error"]))
                stop = (message.get("result") or {}).get("stopReason")
                if stop and _norm(stop) not in {_norm(s) for s in _STOP_OK}:
                    if _norm(stop) == "refusal":
                        raise AcpError("the model refused this request")
                    if _norm(stop) == "cancelled":
                        raise AcpError("the turn was cancelled")
                return

            # A request from the agent.
            if message.get("method") and message.get("id") is not None:
                self._answer_agent_request(message)
                await self._flush()
                continue

            # A notification.
            method = _norm(message.get("method"))
            if method not in ("sessionupdate", "sessionnotification"):
                continue

            params = message.get("params") or {}
            update = _update_of(params)
            kind = _kind_of(update)

            if kind == _THOUGHT:
                chunk = _blocks_to_text(update.get("content") or update.get("text"))
                if chunk:
                    yield ("thought", chunk)
            elif kind == _MESSAGE:
                chunk = _blocks_to_text(update.get("content") or update.get("text"))
                if chunk:
                    yield ("text", chunk)
            elif kind == _TURN_END:
                return


async def stream_turn(
    prompt: str,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> AsyncIterator[Tuple[str, str]]:
    """Run one prompt through a fresh ACP process, streaming as it arrives."""
    limit = timeout or settings.request_timeout
    client = AcpClient()
    await client.start()
    try:
        await client.handshake(timeout=min(limit, 60))
        if model:
            await client.set_model(model, timeout=min(limit, 30))
        async for item in client.stream(prompt, timeout=limit):
            yield item
    finally:
        await client.close()
