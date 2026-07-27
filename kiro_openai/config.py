from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _env_list(name: str) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # Path to the Kiro CLI binary.
    cli_bin: str = field(default_factory=lambda: os.getenv("KIRO_CLI_BIN", "kiro-cli"))

    # Directory the CLI runs in. Point this at a throwaway dir: the CLI is an
    # agent and will read the working tree as context if tools are trusted.
    workdir: str = field(default_factory=lambda: os.getenv("KIRO_BRIDGE_WORKDIR", "/tmp/kiro-bridge"))

    # Tool categories auto-approved per request, e.g. "read,grep".
    # Empty (the default) means no tools are trusted -> behaves like a plain LLM.
    trust_tools: List[str] = field(default_factory=lambda: _env_list("KIRO_TRUST_TOOLS"))

    # Named agent config to run under (--agent). Optional.
    agent: Optional[str] = field(default_factory=lambda: os.getenv("KIRO_AGENT") or None)

    # Bearer token clients must present. Unset = open (bind to localhost only!).
    bridge_api_key: Optional[str] = field(default_factory=lambda: os.getenv("BRIDGE_API_KEY") or None)

    # Model used when the request omits one / asks for an unknown id.
    default_model: str = field(default_factory=lambda: os.getenv("KIRO_DEFAULT_MODEL", "auto"))

    request_timeout: float = field(default_factory=lambda: float(os.getenv("KIRO_TIMEOUT_SECONDS", "300")))
    max_concurrency: int = field(default_factory=lambda: int(os.getenv("KIRO_MAX_CONCURRENCY", "4")))

    # Prompts longer than this are piped on stdin instead of passed via argv,
    # to stay clear of the kernel's argument-length limit.
    argv_prompt_limit: int = field(default_factory=lambda: int(os.getenv("KIRO_ARGV_PROMPT_LIMIT", "8000")))

    # Bytes emitted per SSE chunk when streaming.
    stream_chunk_size: int = field(default_factory=lambda: int(os.getenv("KIRO_STREAM_CHUNK_SIZE", "24")))


settings = Settings()
