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

    # Web console ----------------------------------------------------------
    brand: str = field(default_factory=lambda: os.getenv("BRAND", "RioApis"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "5000")))

    # IPs allowed to administer the console. Written by setup.sh; these cannot
    # be removed from the web UI. Additional IPs are managed in the database.
    admin_ips: List[str] = field(default_factory=lambda: _env_list("ADMIN_WHITELIST_IPS"))

    # Kiro charges per request, scaled by the model's credit multiplier.
    # Pay-as-you-go overage is $0.04/credit.
    # https://kiro.dev/blog/new-pricing-plans-and-auto/
    usd_per_credit: float = field(default_factory=lambda: float(os.getenv("USD_PER_CREDIT", "0.04")))

    # Usage log database.
    usage_db_path: str = field(
        default_factory=lambda: os.getenv(
            "USAGE_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "usage.db")
        )
    )

    # Where .env lives, so the settings page can persist changes.
    env_file: str = field(
        default_factory=lambda: os.getenv(
            "ENV_FILE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        )
    )

    enable_web_ui: bool = field(
        default_factory=lambda: os.getenv("ENABLE_WEB_UI", "true").lower() not in ("0", "false", "no")
    )

    # Trust X-Forwarded-For for client IPs.
    #
    # Off by default and deliberately so: console access is decided by caller
    # IP, and any client can set this header. Enabling it without a proxy that
    # overwrites the header would let anyone claim a whitelisted address.
    trust_proxy_headers: bool = field(
        default_factory=lambda: os.getenv("TRUST_PROXY_HEADERS", "false").lower() in ("1", "true", "yes")
    )

    request_timeout: float = field(default_factory=lambda: float(os.getenv("KIRO_TIMEOUT_SECONDS", "300")))
    max_concurrency: int = field(default_factory=lambda: int(os.getenv("KIRO_MAX_CONCURRENCY", "4")))

    # Prompts longer than this are piped on stdin instead of passed via argv,
    # to stay clear of the kernel's argument-length limit.
    argv_prompt_limit: int = field(default_factory=lambda: int(os.getenv("KIRO_ARGV_PROMPT_LIMIT", "8000")))

    # Bytes emitted per SSE chunk when streaming.
    stream_chunk_size: int = field(default_factory=lambda: int(os.getenv("KIRO_STREAM_CHUNK_SIZE", "24")))

    def admin_ip_set(self) -> set:
        """Root admin IPs. Loopback is always included so curl works locally."""
        return set(self.admin_ips) | {"127.0.0.1", "::1", "localhost", "testclient"}


settings = Settings()
