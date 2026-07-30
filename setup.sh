#!/usr/bin/env bash
#
# Interactive installer for the Kiro OpenAI-compatible bridge.
#
#   ./setup.sh              guided install
#   ./setup.sh --yes        non-interactive (reads KIRO_API_KEY from env)
#   ./setup.sh --help       usage
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV_DIR="$ROOT/.venv"
ENV_FILE="$ROOT/.env"
LOG_FILE="$(mktemp -t kiro-setup.XXXXXX.log)"

ASSUME_YES=0
FORCE=0
SKIP_CLI_INSTALL=0
SKIP_VERIFY=0

# --------------------------------------------------------------------- palette

setup_colors() {
    local ncolors=0
    if command -v tput >/dev/null 2>&1; then
        ncolors="$(tput colors 2>/dev/null || echo 0)"
    fi

    if [[ -n "${NO_COLOR:-}" || "${TERM:-dumb}" == "dumb" || ! -t 1 || "$ncolors" -lt 8 ]]; then
        BOLD=""; DIM=""; RESET=""
        RED=""; GREEN=""; YELLOW=""; BLUE=""; MAGENTA=""; CYAN=""; GREY=""
        C1=""; C2=""; C3=""; C4=""; C5=""; C6=""
        return
    fi

    BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    BLUE=$'\033[34m'; MAGENTA=$'\033[35m'; CYAN=$'\033[36m'; GREY=$'\033[90m'

    if [[ "$ncolors" -ge 256 ]]; then
        # Violet -> cyan gradient for the banner.
        C1=$'\033[38;5;99m';  C2=$'\033[38;5;105m'; C3=$'\033[38;5;111m'
        C4=$'\033[38;5;117m'; C5=$'\033[38;5;123m'; C6=$'\033[38;5;159m'
    else
        C1="$MAGENTA"; C2="$MAGENTA"; C3="$BLUE"
        C4="$BLUE";    C5="$CYAN";    C6="$CYAN"
    fi
}

supports_unicode() {
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
        *[Uu][Tt][Ff]*) return 0 ;;
        *) return 1 ;;
    esac
}

setup_glyphs() {
    if supports_unicode; then
        G_OK="✔"; G_NO="✘"; G_WARN="!"; G_ARROW="›"; G_DOT="•"
        G_TL="╭"; G_TR="╮"; G_BL="╰"; G_BR="╯"; G_H="─"; G_V="│"
        SPIN_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    else
        G_OK="+"; G_NO="x"; G_WARN="!"; G_ARROW=">"; G_DOT="*"
        G_TL="+"; G_TR="+"; G_BL="+"; G_BR="+"; G_H="-"; G_V="|"
        SPIN_FRAMES=('|' '/' '-' '\')
    fi
}

# ---------------------------------------------------------------------- banner

banner() {
    printf '\n'
    if supports_unicode; then
        printf '%s  ██████╗ ██╗ ██████╗  █████╗ ██████╗ ██╗███████╗%s\n' "$C1" "$RESET"
        printf '%s  ██╔══██╗██║██╔═══██╗██╔══██╗██╔══██╗██║██╔════╝%s\n' "$C2" "$RESET"
        printf '%s  ██████╔╝██║██║   ██║███████║██████╔╝██║███████╗%s\n' "$C3" "$RESET"
        printf '%s  ██╔══██╗██║██║   ██║██╔══██║██╔═══╝ ██║╚════██║%s\n' "$C4" "$RESET"
        printf '%s  ██║  ██║██║╚██████╔╝██║  ██║██║     ██║███████║%s\n' "$C5" "$RESET"
        printf '%s  ╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝%s\n' "$C6" "$RESET"
    else
        printf '%s   ____  _       _    ____  _%s\n' "$C1" "$RESET"
        printf '%s  |  _ \\(_) ___ / \\  |  _ \\(_)___%s\n' "$C2" "$RESET"
        printf '%s  | |_) | |/ _ \\ _ \\ | |_) | / __|%s\n' "$C3" "$RESET"
        printf '%s  |  _ <| | (_) | | ||  __/| \\__ \\%s\n' "$C4" "$RESET"
        printf '%s  |_| \\_\\_|\\___/_| |_|_|   |_|___/%s\n' "$C5" "$RESET"
    fi
    printf '\n'
    printf '  %sRioApis%s %s%s%s %sOpenAI-compatible API on the Kiro CLI%s\n' \
        "$BOLD" "$RESET" "$GREY" "$G_DOT" "$RESET" "$DIM" "$RESET"
    printf '\n'
}

# ------------------------------------------------------------------------- ui

STEP_NUM=0
STEP_TOTAL=6

step() {
    STEP_NUM=$((STEP_NUM + 1))
    printf '\n%s%s[%d/%d]%s %s%s%s\n' \
        "$BOLD" "$CYAN" "$STEP_NUM" "$STEP_TOTAL" "$RESET" "$BOLD" "$1" "$RESET"
}

ok()   { printf '  %s%s%s %s\n' "$GREEN" "$G_OK" "$RESET" "$1"; }
bad()  { printf '  %s%s%s %s\n' "$RED" "$G_NO" "$RESET" "$1"; }
warn() { printf '  %s%s%s %s\n' "$YELLOW" "$G_WARN" "$RESET" "$1"; }
info() { printf '  %s%s%s %s\n' "$BLUE" "$G_ARROW" "$RESET" "$1"; }
note() { printf '    %s%s%s\n' "$GREY" "$1" "$RESET"; }

die() {
    printf '\n  %s%s%s %s%s%s\n' "$RED" "$G_NO" "$RESET" "$BOLD" "$1" "$RESET"
    if [[ -s "$LOG_FILE" ]]; then
        printf '\n  %slast lines of %s:%s\n' "$GREY" "$LOG_FILE" "$RESET"
        tail -n 15 "$LOG_FILE" | sed "s/^/    ${GREY}/;s/\$/${RESET}/"
    fi
    printf '\n'
    exit 1
}

# Run a command quietly with a spinner; dump the log only on failure.
run_step() {
    local label="$1"; shift
    if [[ ! -t 1 ]]; then
        printf '  %s%s%s %s ... ' "$BLUE" "$G_ARROW" "$RESET" "$label"
        if "$@" >>"$LOG_FILE" 2>&1; then printf 'ok\n'; return 0; fi
        printf 'FAILED\n'; return 1
    fi

    "$@" >>"$LOG_FILE" 2>&1 &
    local pid=$! i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf '\r  %s%s%s %s' "$MAGENTA" "${SPIN_FRAMES[$((i % ${#SPIN_FRAMES[@]}))]}" "$RESET" "$label"
        i=$((i + 1))
        sleep 0.1
    done
    if wait "$pid"; then
        printf '\r  %s%s%s %s\033[K\n' "$GREEN" "$G_OK" "$RESET" "$label"
        return 0
    fi
    printf '\r  %s%s%s %s\033[K\n' "$RED" "$G_NO" "$RESET" "$label"
    return 1
}

rule() {
    local width=54 line=""
    local i
    for ((i = 0; i < width; i++)); do line="${line}${G_H}"; done
    printf '  %s%s%s\n' "$GREY" "$line" "$RESET"
}

# Prompts read from /dev/tty so they still work when stdout is piped. Where no
# terminal exists at all (CI, docker build), every prompt silently takes its
# default instead of erroring.
TTY_OK=0
if [[ -r /dev/tty ]] && : >/dev/tty 2>/dev/null; then
    TTY_OK=1
fi

interactive() {
    [[ "$ASSUME_YES" == "0" && "$TTY_OK" == "1" ]]
}

ask() {
    # ask <prompt> <default> -> echoes answer on stdout
    local prompt="$1" default="${2:-}" reply=""
    if ! interactive; then
        printf '%s' "$default"
        return
    fi
    if [[ -n "$default" ]]; then
        read -r -p "$(printf '  %s%s%s %s %s[%s]%s ' \
            "$MAGENTA" "$G_ARROW" "$RESET" "$prompt" "$GREY" "$default" "$RESET")" \
            reply </dev/tty || true
    else
        read -r -p "$(printf '  %s%s%s %s ' "$MAGENTA" "$G_ARROW" "$RESET" "$prompt")" \
            reply </dev/tty || true
    fi
    printf '%s' "${reply:-$default}"
}

confirm() {
    # confirm <prompt> <default y|n>
    local prompt="$1" default="${2:-y}" reply
    interactive || { [[ "$default" == "y" ]]; return; }
    local hint="y/N"; [[ "$default" == "y" ]] && hint="Y/n"
    read -r -p "$(printf '  %s%s%s %s %s[%s]%s ' \
        "$MAGENTA" "$G_ARROW" "$RESET" "$prompt" "$GREY" "$hint" "$RESET")" \
        reply </dev/tty || true
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

ask_secret() {
    # ask_secret <prompt> -> echoes secret on stdout (input hidden)
    local prompt="$1" reply=""
    interactive || { printf ''; return; }
    read -r -s -p "$(printf '  %s%s%s %s ' "$MAGENTA" "$G_ARROW" "$RESET" "$prompt")" \
        reply </dev/tty || true
    printf '\n' >&2
    printf '%s' "$reply"
}

mask() {
    local s="$1"
    local n=${#s}
    if (( n <= 10 )); then printf '%s' "****"; else printf '%s...%s' "${s:0:8}" "${s: -4}"; fi
}

# ----------------------------------------------------------------------- usage

usage() {
    cat <<EOF
Kiro OpenAI bridge installer

Usage: ./setup.sh [options]

  -y, --yes            Non-interactive. Uses \$KIRO_API_KEY, generates a
                       bridge token, and accepts all defaults.
  -f, --force          Overwrite an existing .env without asking.
      --no-cli         Do not offer to install the Kiro CLI.
      --no-verify      Skip the post-install self-test.
      --no-color       Disable colored output.
  -h, --help           Show this message.

Environment:
  KIRO_API_KEY         Kiro CLI key (ksk_...) from app.kiro.dev -> API Keys.
  BRIDGE_API_KEY       Token your OpenAI clients will send. Generated if unset.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)    ASSUME_YES=1 ;;
        -f|--force)  FORCE=1 ;;
        --no-cli)    SKIP_CLI_INSTALL=1 ;;
        --no-verify) SKIP_VERIFY=1 ;;
        --no-color)  NO_COLOR=1 ;;
        -h|--help)   usage; exit 0 ;;
        *) printf 'unknown option: %s\n\n' "$1"; usage; exit 2 ;;
    esac
    shift
done

setup_colors
setup_glyphs
banner

note "log: $LOG_FILE"

# ------------------------------------------------------- 1. host prerequisites

step "Checking prerequisites"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    fi
done

[[ -n "$PYTHON_BIN" ]] || die "Python 3.9+ is required but was not found on PATH."
ok "python $("$PYTHON_BIN" -c 'import platform; print(platform.python_version())') $(printf '%s(%s)%s' "$GREY" "$PYTHON_BIN" "$RESET")"

"$PYTHON_BIN" -c 'import venv' 2>/dev/null \
    || die "The 'venv' module is missing. On Debian/Ubuntu: apt install python3-venv"
ok "venv module available"

if command -v curl >/dev/null 2>&1; then
    ok "curl present"
else
    warn "curl not found (only needed to auto-install the Kiro CLI)"
fi

# --------------------------------------------------------------- 2. kiro cli

step "Locating the Kiro CLI"

KIRO_CLI_BIN="${KIRO_CLI_BIN:-}"
find_kiro_cli() {
    local c
    for c in "$KIRO_CLI_BIN" kiro-cli "$HOME/.local/bin/kiro-cli"; do
        [[ -n "$c" ]] || continue
        if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
        if [[ -x "$c" ]]; then printf '%s' "$c"; return 0; fi
    done
    return 1
}

if CLI_PATH="$(find_kiro_cli)"; then
    ok "kiro-cli found $(printf '%s(%s)%s' "$GREY" "$CLI_PATH" "$RESET")"
else
    CLI_PATH=""
    warn "kiro-cli not found on PATH"
    note "The bridge shells out to the Kiro CLI; there is no HTTP API to call instead."
    if [[ "$SKIP_CLI_INSTALL" == "0" ]] && command -v curl >/dev/null 2>&1 \
        && confirm "Install it now from cli.kiro.dev?" "y"; then
        if run_step "installing kiro-cli" bash -c 'curl -fsSL https://cli.kiro.dev/install | bash'; then
            export PATH="$HOME/.local/bin:$PATH"
            CLI_PATH="$(find_kiro_cli || true)"
            [[ -n "$CLI_PATH" ]] && ok "installed at $CLI_PATH" \
                || warn "installer finished but kiro-cli is still not on PATH"
        else
            warn "install failed; continuing without it"
        fi
    else
        note "Install later: curl -fsSL https://cli.kiro.dev/install | bash"
    fi
fi

# ------------------------------------------------------------ 3. virtualenv

step "Creating the virtual environment"

if [[ -d "$VENV_DIR" ]]; then
    ok "reusing $(printf '%s.venv%s' "$BOLD" "$RESET")"
else
    run_step "python -m venv .venv" "$PYTHON_BIN" -m venv "$VENV_DIR" \
        || die "Could not create the virtual environment."
fi

VENV_PY="$VENV_DIR/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="$VENV_DIR/Scripts/python.exe"
[[ -x "$VENV_PY" ]] || die "Virtual environment looks broken: no python inside $VENV_DIR"

# --------------------------------------------------------- 4. dependencies

step "Installing dependencies"

run_step "upgrading pip" "$VENV_PY" -m pip install --upgrade pip \
    || warn "pip upgrade failed; continuing with the bundled version"

run_step "installing requirements.txt" "$VENV_PY" -m pip install -r "$ROOT/requirements.txt" \
    || die "Dependency installation failed."

for pkg in fastapi uvicorn pydantic; do
    "$VENV_PY" -c "import $pkg" 2>/dev/null || die "Package '$pkg' did not import after install."
done
ok "fastapi, uvicorn and pydantic import cleanly"

# ---------------------------------------------------------- 5. credentials

step "Configuring credentials"

WRITE_ENV=1
if [[ -f "$ENV_FILE" ]]; then
    if [[ "$FORCE" == "1" ]]; then
        warn "overwriting existing .env (--force)"
    elif ! interactive; then
        ok "keeping existing .env"
        WRITE_ENV=0
    elif confirm ".env already exists. Replace it?" "n"; then
        warn "replacing .env"
    else
        ok "keeping existing .env"
        WRITE_ENV=0
    fi
fi

if [[ "$WRITE_ENV" == "1" ]]; then
    KEY_IN="${KIRO_API_KEY:-}"

    if [[ -n "$KEY_IN" ]]; then
        ok "using KIRO_API_KEY from the environment $(printf '%s%s%s' "$GREY" "$(mask "$KEY_IN")" "$RESET")"
    elif ! interactive; then
        warn "KIRO_API_KEY not set; writing a placeholder"
        note "Add it to .env, or run 'kiro-cli login' to use a browser session."
    else
        printf '\n'
        info "Create a key at $(printf '%s%s%s' "$BOLD" "https://app.kiro.dev" "$RESET") under API Keys."
        note "Requires a Pro plan or above. Input is hidden. Leave blank to skip if"
        note "you are already signed in with 'kiro-cli login'."
        printf '\n'
        while true; do
            KEY_IN="$(ask_secret 'Kiro API key:')"
            if [[ -z "$KEY_IN" ]]; then
                warn "no key entered; relying on an existing CLI session"
                break
            fi
            if [[ "$KEY_IN" == ksk_* ]]; then
                ok "key accepted $(printf '%s%s%s' "$GREY" "$(mask "$KEY_IN")" "$RESET")"
                break
            fi
            bad "Kiro keys start with 'ksk_'. Try again, or press Enter to skip."
        done
    fi

    BRIDGE_KEY="${BRIDGE_API_KEY:-}"
    if [[ -z "$BRIDGE_KEY" ]]; then
        BRIDGE_KEY="sk-kiro-$("$VENV_PY" -c 'import secrets; print(secrets.token_hex(20))')"
        ok "generated a bridge token for your OpenAI clients"
    else
        ok "using BRIDGE_API_KEY from the environment"
    fi

    DEFAULT_MODEL="$(ask 'Default model' 'auto')"
    PORT="$(ask 'Port' '5000')"

    # ── console access ───────────────────────────────────────────────
    # The console is restricted by caller IP rather than a password, so the
    # owner's address has to be captured here. It lands in .env, which the web
    # UI cannot edit, so a mistake in the browser cannot lock everyone out.
    DETECTED_IP=""
    if [[ -n "${SSH_CLIENT:-}" ]]; then
        DETECTED_IP="$(printf '%s' "$SSH_CLIENT" | awk '{print $1}')"
    elif [[ -n "${SSH_CONNECTION:-}" ]]; then
        DETECTED_IP="$(printf '%s' "$SSH_CONNECTION" | awk '{print $1}')"
    fi

    printf '\n'
    info "Who administers the RioApis console?"
    note "Only these addresses can open the web UI, generate API keys and read"
    note "usage. Generated keys keep working from any address."
    if [[ -n "$DETECTED_IP" ]]; then
        note "Detected your SSH client address: $DETECTED_IP"
    fi
    printf '\n'

    ADMIN_IPS="${ADMIN_WHITELIST_IPS:-}"
    if [[ -z "$ADMIN_IPS" ]]; then
        ADMIN_IPS="$(ask 'Owner IP (comma-separated for several)' "$DETECTED_IP")"
    fi

    if [[ -z "$ADMIN_IPS" ]]; then
        warn "no owner IP set; the console will only open from localhost"
        note "Add ADMIN_WHITELIST_IPS to .env later, or use an SSH tunnel:"
        note "  ssh -L ${PORT}:127.0.0.1:${PORT} <user>@<host>"
    else
        ok "console restricted to $ADMIN_IPS $(printf '%s(plus localhost)%s' "$GREY" "$RESET")"
    fi

    umask 077
    cat > "$ENV_FILE" <<EOF
# Written by setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)

# Absolute path to the Kiro CLI. Recorded here because the server does not
# inherit the PATH of an interactive login shell (e.g. ~/.local/bin).
KIRO_CLI_BIN=${CLI_PATH:-kiro-cli}

# Kiro CLI credential (app.kiro.dev -> API Keys)
KIRO_API_KEY=${KEY_IN}

# Token your OpenAI clients must send as: Authorization: Bearer <this>
BRIDGE_API_KEY=${BRIDGE_KEY}

KIRO_DEFAULT_MODEL=${DEFAULT_MODEL}
PORT=${PORT}
HOST=0.0.0.0

# Addresses allowed to open the RioApis console. Cannot be changed from the web
# UI, so a mistake there can never lock you out. Localhost is always allowed.
ADMIN_WHITELIST_IPS=${ADMIN_IPS}

# Kiro charges per request scaled by the model's credit weight.
# Pay-as-you-go overage is \$0.04 per credit.
USD_PER_CREDIT=0.04

KIRO_BRIDGE_WORKDIR=/tmp/kiro-bridge
KIRO_TIMEOUT_SECONDS=300
KIRO_MAX_CONCURRENCY=4

# Empty means no tools are trusted, so it behaves like a plain chat model.
KIRO_TRUST_TOOLS=
EOF
    chmod 600 "$ENV_FILE"
    ok ".env written $(printf '%smode 600%s' "$GREY" "$RESET")"
fi

cli_usable() {
    local c="${1:-}"
    [[ -n "$c" ]] || return 1
    command -v "$c" >/dev/null 2>&1 && return 0
    [[ -x "$c" ]] && return 0
    return 1
}

# Repair a kept .env that has no KIRO_CLI_BIN, or one pointing at a binary the
# server cannot resolve. Without this the API answers every request with
# "Kiro CLI not found".
if [[ -n "$CLI_PATH" && -f "$ENV_FILE" ]]; then
    CURRENT_CLI="$(sed -n 's/^KIRO_CLI_BIN=//p' "$ENV_FILE" | tail -n 1)"
    if ! cli_usable "$CURRENT_CLI"; then
        if grep -q '^KIRO_CLI_BIN=' "$ENV_FILE"; then
            sed -i.bak "s|^KIRO_CLI_BIN=.*|KIRO_CLI_BIN=${CLI_PATH}|" "$ENV_FILE"
            rm -f "$ENV_FILE.bak"
        else
            printf 'KIRO_CLI_BIN=%s\n' "$CLI_PATH" >> "$ENV_FILE"
        fi
        ok "recorded kiro-cli path in .env $(printf '%s(%s)%s' "$GREY" "$CLI_PATH" "$RESET")"
    fi
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

# -------------------------------------------------------------- 6. self-test

step "Verifying the install"

if [[ "$SKIP_VERIFY" == "1" ]]; then
    warn "skipped (--no-verify)"
else
    if "$VENV_PY" -m pip show httpx >/dev/null 2>&1; then
        :
    else
        run_step "installing httpx for the self-test" "$VENV_PY" -m pip install httpx || true
    fi

    if run_step "self-test against the stub CLI" "$VENV_PY" "$ROOT/tools/verify.py"; then
        ok "HTTP surface behaves like the OpenAI API"
    else
        warn "self-test failed; see $LOG_FILE"
    fi

    if [[ -n "$CLI_PATH" ]]; then
        # Deliberately no KIRO_CLI_BIN override here: this must exercise the
        # exact configuration run.sh will use, or a .env missing the CLI path
        # passes the self-test and then fails at runtime.
        if run_step "querying real models via kiro-cli" \
            "$VENV_PY" -c '
import asyncio, sys
from kiro_openai.backend import backend
async def main():
    await backend.startup()
    print("models:", backend.models())
asyncio.run(main())
'; then
            ok "kiro-cli responded"
        else
            warn "could not reach kiro-cli; check that your key is valid"
        fi
    else
        warn "kiro-cli missing, so live inference was not tested"
    fi
fi

# ------------------------------------------------------------------- summary

PORT="${PORT:-5000}"
# `|| true` matters: pipefail makes a failing `hostname -I` fatal under `set -e`.
PUBLIC_HOST="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[[ -n "$PUBLIC_HOST" ]] || PUBLIC_HOST="127.0.0.1"

printf '\n'
printf '  %s%s%s Setup complete%s\n' "$GREEN" "$BOLD" "$G_OK" "$RESET"
printf '\n'
rule
printf '  %sstart%s      %s./run.sh%s\n' "$GREY" "$RESET" "$BOLD" "$RESET"
printf '  %sconsole%s    %shttp://%s:%s%s\n' "$GREY" "$RESET" "$CYAN" "$PUBLIC_HOST" "$PORT" "$RESET"
printf '  %sapi%s        %shttp://%s:%s/v1%s\n' "$GREY" "$RESET" "$CYAN" "$PUBLIC_HOST" "$PORT" "$RESET"
printf '  %saccess%s     %s%s%s\n' "$GREY" "$RESET" "$DIM" "${ADMIN_WHITELIST_IPS:-localhost only}" "$RESET"
rule
printf '\n'
printf '  Open the console, then %sAPI keys → Generate key%s to mint an\n' "$BOLD" "$RESET"
printf '  OpenAI-compatible key. Keys work from any address.\n'
printf '\n'
