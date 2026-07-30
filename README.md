# RioApis

An OpenAI-compatible API served from the Kiro CLI, with a web console for
chatting, minting API keys and tracking spend.

Kiro has no public REST API. Its only programmatic surfaces are the
[headless CLI](https://kiro.dev/docs/cli/headless) and
[ACP over stdio](https://kiro.dev/docs/cli/acp). RioApis translates
`POST /v1/chat/completions` into `kiro-cli chat --no-interactive` and maps the
result back into OpenAI's response shape.

## Install

```bash
git clone https://github.com/TrishaAdio/api.git
cd api
./setup.sh
./run.sh
```

The installer checks prerequisites, offers to install the Kiro CLI, creates
`.venv`, installs dependencies, asks for your Kiro API key and for the IP
allowed to administer the console, then writes `.env` at mode 600 and runs a
self-test.

Then open `http://<server>:5000` and go to **API keys → Generate key**.

## Two trust boundaries

|                | Console (`/`, `/api/*`)      | API (`/v1/*`)              |
| -------------- | ---------------------------- | -------------------------- |
| Authenticated by | your IP address            | a generated API key        |
| Who can reach it | whitelisted addresses only  | anyone with a valid key    |
| Managed in     | `.env` + the Access page     | the API keys page          |

The owner IP goes into `ADMIN_WHITELIST_IPS` in `.env`, which the web UI cannot
edit — a mistake in the browser can never lock you out. Additional addresses are
added on the **Access** page. Localhost always has access, so an SSH tunnel
works as a fallback:

```bash
ssh -L 5000:127.0.0.1:5000 user@server
```

API keys are shown once and stored as SHA-256 hashes. They work from any
address and can be revoked or deleted individually.

> **`TRUST_PROXY_HEADERS` is off by default.** Console access is decided by
> caller IP, and any client can send `X-Forwarded-For`. Only enable it behind a
> reverse proxy that overwrites the header, or anyone can claim a whitelisted
> address.

## Console

- **Chat** — pick any model your account offers and talk to it.
- **Usage** — spend, requests, tokens, unique callers and latency; a 24-hour
  chart; breakdowns by model and by IP; and a filterable request log showing
  which IP used which model at what cost.
- **API keys** — generate, revoke and delete keys; see per-key request counts
  and spend.
- **Access** — manage the console IP whitelist.
- **Docs** — endpoints and copy-paste curl / Python / Node examples.
- **Settings** — Kiro API key, default model, trusted tools, price per credit.

## Using the API

```python
from openai import OpenAI

client = OpenAI(base_url="http://<server>:5000/v1", api_key="sk-rio-…")
client.chat.completions.create(
    model="claude-sonnet-5",
    messages=[{"role": "user", "content": "hello"}],
)
```

| Method | Path                   | Notes                          |
| ------ | ---------------------- | ------------------------------ |
| `POST` | `/v1/chat/completions` | Streaming and non-streaming    |
| `GET`  | `/v1/models`           | Discovered from the Kiro CLI   |
| `GET`  | `/v1/models/{id}`      |                                |
| `GET`  | `/healthz`             | No auth                        |

The model you name is the model that answers. An unknown id returns **404**
listing what is available, rather than quietly serving something else. Omitting
`model` uses `KIRO_DEFAULT_MODEL`.

Per-request selection needs a CLI build whose `chat` subcommand accepts
`--model`; check `model_selection` in `/healthz`. If it is `false`, set a
default instead:

```bash
kiro-cli settings chat.defaultModel claude-sonnet-5
```

## How spend is calculated

Kiro bills **per request**, scaled by each model's credit weight, and
pay-as-you-go overage is
[$0.04 per credit](https://kiro.dev/blog/new-pricing-plans-and-auto/). One call
to a 1.30x model therefore costs 1.30 credits, or $0.052. Adjust the rate in
Settings if your plan differs.

Failed requests are recorded at zero. The Kiro CLI reports no billing data, so
these figures are close estimates, not invoices.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `KIRO_API_KEY` | — | Kiro credential (`ksk_…`) |
| `KIRO_CLI_BIN` | detected | Absolute path to `kiro-cli` |
| `ADMIN_WHITELIST_IPS` | — | Addresses allowed into the console |
| `USD_PER_CREDIT` | `0.04` | Rate used to price usage |
| `BRIDGE_API_KEY` | — | Bootstrap API key, valid before you mint one |
| `KIRO_DEFAULT_MODEL` | `auto` | Used when a request omits `model` |
| `KIRO_TRUST_TOOLS` | empty | Tool categories to auto-approve, e.g. `read,grep` |
| `TRUST_PROXY_HEADERS` | `false` | Honour `X-Forwarded-For` |
| `HOST` / `PORT` | `0.0.0.0` / `5000` | Bind address |
| `USAGE_DB_PATH` | `./usage.db` | SQLite database |

`KIRO_TRUST_TOOLS` is empty by default, so RioApis behaves like a plain chat
model rather than an agent with access to the filesystem.

## Limitations

- **Streaming is replayed, not live.** `--no-interactive` returns the whole
  answer at once, so chunks are emitted after generation finishes. The protocol
  is correct; time-to-first-token is not improved.
- **`temperature`, `top_p`, `max_tokens` and `stop` are accepted but ignored.**
  The CLI exposes no equivalent controls.
- **Token counts are estimates.** The CLI reports none.
- Images in `content` parts are replaced with a placeholder.

## Tests

```bash
.venv/bin/python tools/verify.py                    # OpenAI wire contract
.venv/bin/python tools/verify_sdk.py                # via the real openai SDK
.venv/bin/python tools/verify_models.py             # per-request model selection
TEXT_ONLY=1 .venv/bin/python tools/verify_models.py # discovery from the text table
.venv/bin/python tools/verify_no_model_flag.py      # CLI without --model
.venv/bin/python tools/verify_web.py                # console, keys, whitelist, spend
```

`tools/fake-kiro-cli` stands in for the real CLI so tests consume no credits.
