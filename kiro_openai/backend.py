from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import List, Optional, Sequence, Set

from .config import settings

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SPINNER_RE = re.compile(r"^[\s⠁-⣿]*$")

_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

_FALLBACK_MODELS = ("auto",)


class KiroError(RuntimeError):
    """Raised when the Kiro CLI cannot be run or exits unsuccessfully."""

    def __init__(self, message: str, exit_code: Optional[int] = None):
        super().__init__(message)
        self.exit_code = exit_code


class ModelNotAvailable(KiroError):
    """The requested model cannot serve the request.

    Raised instead of quietly substituting a different model, so a client that
    names a model always either gets that model or an explicit error.
    """


def _clean(raw: str) -> str:
    """Strip ANSI escapes and residual spinner-only lines from CLI output."""
    text = _ANSI_RE.sub("", raw)
    text = text.replace("\r", "\n")
    kept = [line for line in text.split("\n") if not (line.strip() and _SPINNER_RE.match(line))]
    return "\n".join(kept).strip()


def _strip_response_marker(text: str) -> str:
    """Remove the '> ' marker the CLI prints ahead of its answer.

    Only the very start of the output is touched, so genuine markdown
    blockquotes later in the response survive intact.
    """
    if text.startswith("> "):
        return text[2:].lstrip("\n")
    if text.startswith(">\n"):
        return text[2:].lstrip("\n")
    return text


def _child_env() -> dict:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["KIRO_LOG_NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


class KiroBackend:
    """Spawns `kiro-cli chat --no-interactive` and adapts it to a chat API."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._chat_flags: Set[str] = set()
        self._models: List[str] = []
        self._ready = False

    # ---------------------------------------------------------------- startup

    async def startup(self) -> None:
        if shutil.which(settings.cli_bin) is None and not os.path.isfile(settings.cli_bin):
            raise KiroError(
                "Kiro CLI not found at {0!r}. Install it with "
                "`curl -fsSL https://cli.kiro.dev/install | bash` or set KIRO_CLI_BIN.".format(settings.cli_bin)
            )

        os.makedirs(settings.workdir, exist_ok=True)

        self._chat_flags = await self._probe_chat_flags()
        self._models = await self._probe_models()
        self._ready = True

    @property
    def ready(self) -> bool:
        return self._ready

    async def _probe_chat_flags(self) -> Set[str]:
        """Read `chat --help` so we only pass flags this CLI build accepts."""
        try:
            code, out, err = await self._exec([settings.cli_bin, "chat", "--help"], timeout=30)
        except KiroError:
            return set()
        if code != 0:
            return set()
        return set(re.findall(r"--[a-z][a-z0-9-]+", _clean(out + "\n" + err)))

    async def _probe_models(self) -> List[str]:
        """Discover model ids, preferring JSON but tolerating the text table.

        `--format json` is not honoured on every build, and when it is not the
        CLI prints its human-readable table instead. Parsing only JSON meant
        model discovery silently collapsed to the fallback list.
        """
        ids: List[str] = []

        try:
            code, out, _ = await self._exec(
                [settings.cli_bin, "chat", "--list-models", "--format", "json"], timeout=60
            )
        except KiroError:
            code, out = 1, ""

        if code == 0:
            cleaned = _clean(out)
            ids = _parse_model_ids(cleaned) or _parse_model_table(cleaned)

        if not ids:
            try:
                code, out, _ = await self._exec(
                    [settings.cli_bin, "chat", "--list-models"], timeout=60
                )
            except KiroError:
                code, out = 1, ""
            if code == 0:
                ids = _parse_model_table(_clean(out))

        if not ids:
            return list(_FALLBACK_MODELS)

        if settings.default_model not in ids:
            ids.insert(0, settings.default_model)
        return ids

    # ----------------------------------------------------------------- public

    def models(self) -> List[str]:
        return list(self._models) if self._models else list(_FALLBACK_MODELS)

    @property
    def supports_model_flag(self) -> bool:
        """Whether this CLI build can select a model per invocation."""
        return "--model" in self._chat_flags

    def resolve_model(self, requested: Optional[str]) -> str:
        """Resolve the model that will serve this request.

        A model named in the request is honoured exactly or the request is
        rejected. Substituting a different model would let a client believe it
        was talking to one model while another answered.
        """
        if not requested:
            return settings.default_model

        available = self.models()
        candidate = requested
        if candidate not in available:
            # Tolerate provider-prefixed ids like "kiro/claude-sonnet-5".
            candidate = requested.split("/")[-1]

        if candidate not in available:
            raise ModelNotAvailable(
                "The model '{0}' does not exist. Available models: {1}".format(
                    requested, ", ".join(available)
                )
            )

        if not self.supports_model_flag and candidate != settings.default_model:
            raise ModelNotAvailable(
                "This kiro-cli build has no 'chat --model' flag, so '{0}' cannot be "
                "selected per request. Either set KIRO_DEFAULT_MODEL={0} in .env and "
                "restart, or run 'kiro-cli settings chat.defaultModel {0}'.".format(candidate)
            )

        return candidate

    async def complete(
        self,
        prompt: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> str:
        argv, stdin_payload = self._build_invocation(prompt, model, effort)

        async with self._semaphore:
            code, out, err = await self._exec(
                argv, timeout=settings.request_timeout, stdin_payload=stdin_payload
            )

        body = _clean(out)
        if code != 0:
            detail = _clean(err) or body or "no output"
            raise KiroError("kiro-cli exited {0}: {1}".format(code, detail[:2000]), exit_code=code)
        if not body:
            raise KiroError("kiro-cli produced no output. stderr: {0}".format(_clean(err)[:2000]))
        return _strip_response_marker(body)

    # ---------------------------------------------------------------- internals

    def _build_invocation(
        self,
        prompt: str,
        model: Optional[str],
        effort: Optional[str],
    ):
        argv: List[str] = [settings.cli_bin, "chat", "--no-interactive"]

        if "--wrap" in self._chat_flags:
            # Without this the CLI hard-wraps output to terminal width, which
            # corrupts code blocks and long lines.
            argv += ["--wrap", "never"]

        resolved_model = self.resolve_model(model)
        if "--model" in self._chat_flags and resolved_model:
            argv += ["--model", resolved_model]

        if effort and effort in _VALID_EFFORTS and "--effort" in self._chat_flags:
            argv += ["--effort", effort]

        if settings.agent and "--agent" in self._chat_flags:
            argv += ["--agent", settings.agent]

        if settings.trust_tools and "--trust-tools" in self._chat_flags:
            argv += ["--trust-tools={0}".format(",".join(settings.trust_tools))]

        stdin_payload: Optional[bytes] = None
        if len(prompt) > settings.argv_prompt_limit:
            # Long transcripts go on stdin; argv carries only the directive.
            stdin_payload = prompt.encode("utf-8")
            argv.append(
                "The conversation transcript and instructions were piped to you on stdin. "
                "Follow them and reply only as the assistant's next message."
            )
        else:
            argv.append(prompt)

        return argv, stdin_payload

    async def _exec(
        self,
        argv: Sequence[str],
        timeout: float,
        stdin_payload: Optional[bytes] = None,
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=settings.workdir if os.path.isdir(settings.workdir) else None,
                env=_child_env(),
            )
        except FileNotFoundError as exc:
            raise KiroError("cannot execute {0!r}: {1}".format(argv[0], exc))

        # Always pass bytes, never None: asyncio.communicate(input=None) leaves
        # the child's stdin open, and the CLI blocks forever reading it.
        payload = stdin_payload if stdin_payload is not None else b""

        try:
            out, err = await asyncio.wait_for(proc.communicate(input=payload), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise KiroError("kiro-cli timed out after {0:.0f}s".format(timeout))

        return (
            proc.returncode,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )


_TABLE_ROW_RE = re.compile(
    r"^\s*(?P<default>\*)?\s*(?P<id>[A-Za-z][\w.\-]*)\s+\d+(?:\.\d+)?x\s+credits\b"
)


def _parse_model_table(raw: str) -> List[str]:
    """Extract model ids from the CLI's human-readable --list-models table.

        Available models (* = default):

        * auto              1.00x credits      Models chosen by task...
          claude-sonnet-5   1.30x credits      Claude Sonnet 5 model...

    Requiring the 'Nx credits' column keeps the header and any trailing prose
    from being mistaken for model ids.
    """
    ids: List[str] = []
    for line in raw.splitlines():
        match = _TABLE_ROW_RE.match(line)
        if not match:
            continue
        model_id = match.group("id")
        if model_id not in ids:
            ids.append(model_id)
    return ids


def _parse_model_ids(raw: str) -> List[str]:
    """Extract model ids from `--list-models --format json` output.

    The exact JSON shape is not contractual, so several plausible shapes are
    handled and anything unrecognised degrades to an empty list.
    """
    start = raw.find("[")
    brace = raw.find("{")
    if brace != -1 and (start == -1 or brace < start):
        start = brace
    if start == -1:
        return []

    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError:
        return []

    if isinstance(data, dict):
        for key in ("models", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []

    if not isinstance(data, list):
        return []

    ids: List[str] = []
    for entry in data:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict):
            candidate = entry.get("id") or entry.get("model") or entry.get("name")
        else:
            continue
        if candidate and candidate not in ids:
            ids.append(str(candidate))
    return ids


backend = KiroBackend()
