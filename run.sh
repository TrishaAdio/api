#!/usr/bin/env bash
#
# Start the bridge using the virtualenv and .env produced by ./setup.sh.
# Any extra arguments are forwarded to uvicorn.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    BOLD=$'\033[1m'; CYAN=$'\033[36m'; GREY=$'\033[90m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; CYAN=""; GREY=""; RED=""; RESET=""
fi

VENV_PY="$ROOT/.venv/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="$ROOT/.venv/Scripts/python.exe"
if [[ ! -x "$VENV_PY" ]]; then
    printf '%serror%s no virtualenv found. Run %s./setup.sh%s first.\n' "$RED" "$RESET" "$BOLD" "$RESET"
    exit 1
fi

if [[ -f "$ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
else
    printf '%swarning%s no .env found; relying on the ambient environment.\n' "$GREY" "$RESET"
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

printf '\n  %sKiro OpenAI bridge%s  %s%s%s\n' "$BOLD" "$RESET" "$CYAN" "http://$HOST:$PORT/v1" "$RESET"
printf '  %sctrl-c to stop%s\n\n' "$GREY" "$RESET"

exec "$VENV_PY" -m uvicorn kiro_openai.server:app \
    --host "$HOST" --port "$PORT" "$@"
