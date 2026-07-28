# kiro-openai-bridge

An OpenAI-compatible HTTP API backed by the Kiro CLI. Point any OpenAI client at
it and Kiro answers.

Kiro has no public REST API. Its only programmatic surfaces are the
[headless CLI](https://kiro.dev/docs/cli/headless) and
[ACP over stdio](https://kiro.dev/docs/cli/acp), and there is no
bring-your-own-endpoint setting. This service translates
`POST /v1/chat/completions` into `kiro-cli chat --no-interactive` and maps the
result back into OpenAI's response shape.

## Install

```bash
git clone https://github.com/TrishaAdio/api.git
cd api
./setup.sh
```

The installer checks prerequisites, offers to install the Kiro CLI, creates
`.venv`, installs dependencies, prompts for your Kiro API key (hidden input,
validated), generates a bridge token, writes `.env` with mode 600, and runs a
self-test.

Non-interactive:

```bash
KIRO_API_KEY=ksk_... ./setup.sh --yes
```

| Flag | Effect |
| --- | --- |
| `-y, --yes` | Accept defaults, read `KIRO_API_KEY` from the environment |
| `-f, --force` | Overwrite an existing `.env` |
| `--no-cli` | Never offer to install the Kiro CLI |
| `--no-verify` | Skip the self-test |
| `--no-color` | Plain output |

Get a key at [app.kiro.dev](https://app.kiro.dev) under **API Keys** (Pro plan or
above). Alternatively run `kiro-cli login` and leave the key blank.

## Run

```bash
./run.sh
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<BRIDGE_API_KEY>")
client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "hello"}],
)
```

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Streaming and non-streaming |
| `GET` | `/v1/models` | Discovered from `kiro-cli chat --list-models` |
| `GET` | `/v1/models/{id}` | |
| `GET` | `/healthz` | No auth; reports CLI status |

## Configuration

Set in `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `KIRO_CLI_BIN` | detected | Absolute path to `kiro-cli`. Set by `setup.sh` |
| `KIRO_API_KEY` | — | Kiro CLI credential |
| `BRIDGE_API_KEY` | generated | Bearer token clients must send |
| `KIRO_DEFAULT_MODEL` | `auto` | Fallback for unknown model ids |
| `KIRO_TRUST_TOOLS` | empty | Tool categories to auto-approve, e.g. `read,grep` |
| `KIRO_BRIDGE_WORKDIR` | `/tmp/kiro-bridge` | Directory the CLI runs in |
| `KIRO_MAX_CONCURRENCY` | `4` | Concurrent CLI processes |
| `KIRO_TIMEOUT_SECONDS` | `300` | Per-request timeout |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address |

`KIRO_TRUST_TOOLS` is empty by default, so the bridge behaves like a plain chat
model. Setting it lets the agent act on `KIRO_BRIDGE_WORKDIR`.

## Choosing a model

List what your account actually offers:

```bash
curl -s http://127.0.0.1:8000/v1/models | python3 -m json.tool
```

Then name it in the request:

```bash
curl -s -H "Authorization: Bearer $BRIDGE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4.5","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:8000/v1/chat/completions
```

The model you name is the model that answers. It is never silently swapped:

- The `model` field is passed to the CLI as `--model` and echoed back in the
  response, plus an `X-Kiro-Model` response header.
- An unknown id returns **404** listing the available models, rather than
  quietly serving a different one.
- Omitting `model` uses `KIRO_DEFAULT_MODEL`.
- Provider-prefixed ids such as `kiro/claude-sonnet-5` are accepted.

Per-request selection needs a CLI build whose `chat` subcommand accepts
`--model`. Check with:

```bash
curl -s http://127.0.0.1:8000/healthz | python3 -c 'import json,sys; print(json.load(sys.stdin)["model_selection"])'
```

If that is `false`, naming a non-default model returns 404 with an explanation
instead of pretending it worked. Set a default globally in that case:

```bash
kiro-cli settings chat.defaultModel claude-sonnet-5
# or, in .env:
KIRO_DEFAULT_MODEL=claude-sonnet-5
```

## Limitations

- **Streaming is replayed, not live.** `--no-interactive` returns the whole
  answer at once, so SSE chunks are emitted after generation finishes. The
  protocol is correct; time-to-first-token is not improved. True streaming needs
  the ACP backend.
- **`temperature`, `top_p`, `max_tokens` and `stop` are accepted but ignored.**
  The CLI exposes no equivalent controls.
- **`usage` counts are estimates** (`len/4`); the CLI reports no token usage.
- Images in `content` parts are replaced with a placeholder.

## Tests

```bash
.venv/bin/python tools/verify.py                  # OpenAI wire contract
.venv/bin/python tools/verify_sdk.py              # same, via the real openai SDK
.venv/bin/python tools/verify_models.py           # per-request model selection
TEXT_ONLY=1 .venv/bin/python tools/verify_models.py   # discovery from the text table
.venv/bin/python tools/verify_no_model_flag.py    # CLI without --model support
```

`tools/fake-kiro-cli` stands in for the real CLI so tests consume no credits.
