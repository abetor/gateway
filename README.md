# tool-gateway

Local Telegram and webhook gateway for CLI tools, with authenticated routing, durable delivery, and JSON audit receipts.

[Quick start](#quick-start) | [Offline demo](#demo) | [Architecture](docs/DESIGN.md) | [Tests](#tests) | [Contributing and agent guide](AGENTS.md) | [MIT license](LICENSE)

## Problem

Local tools often need one narrow path to receive commands and report results.
If every tool implements Telegram credentials, webhook authentication, retries,
deduplication, and logging independently, secrets spread across repositories and
delivery behavior becomes inconsistent.

`tool-gateway` centralizes those boundaries behind one CLI. Other tools call the
CLI as an external process and do not import gateway code or know channel
credentials.

## What it does

- Accepts authenticated HTTP webhooks and dispatches configured commands.
- Polls a Telegram bot, validates the chat allow-list, and routes tasks, replies,
  and answers to configured projects.
- Delivers plain messages or structured version 2 envelopes through Telegram.
- Supports live-message edits, per-class deduplication, availability windows,
  deferred delivery, and transient-failure retries.
- Dispatches bounded, versioned JSON events to configured argv targets without a
  shell and stores replay-safe receipts.
- Builds status snapshots from configured tool and scheduler JSON commands.
- Writes append-only JSONL audit streams for inbound events, dispatches, and
  delivery decisions.

Python 3.11 or newer is required. Runtime code uses only the standard library.

## Architecture

```text
Telegram Bot API                 authenticated webhook
       |                                  |
       v                                  v
 ext/telegram/                       ext/webhook/
       |                                  |
       +--------------+-------------------+
                      v
             core config + registry
                      |
          +-----------+-----------+
          |                       |
          v                       v
  configured CLI project    configured argv target
          |                       |
          +-----------+-----------+
                      v
            <home>/log/*.jsonl
            <home>/state/*
```

The core knows channel contracts, configuration, exit codes, and audit logging.
Concrete transports live under `tool_gateway/ext/`. The gateway may invoke tools
through CLI contracts, but tools depend only on the gateway CLI, never on its
Python package.

See [docs/DESIGN.md](docs/DESIGN.md) for contracts and tradeoffs.

## Quick start

Create an environment and install the checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

Choose an external data directory explicitly. The application has no cwd-based
or hardcoded data-home fallback:

```bash
export GATEWAY_HOME="${GATEWAY_HOME:-$HOME/.local/share/tool-gateway}"
mkdir -p "$GATEWAY_HOME"
```

For a loopback-only webhook, create `$GATEWAY_HOME/gateway.toml`:

```toml
[channels.webhook]
host = "127.0.0.1"
port = 8787
token_env = "GATEWAY_TOKEN"

[routes.example]
command = 'test -f "$GATEWAY_PAYLOAD"'
```

Export the token only in the process environment or place it in
`$GATEWAY_HOME/.env`:

```bash
export GATEWAY_TOKEN='replace-with-a-random-value-at-least-16-characters-long'
tool-gateway --home "$GATEWAY_HOME" doctor
tool-gateway --home "$GATEWAY_HOME" routes
tool-gateway --home "$GATEWAY_HOME" serve
```

For Telegram delivery, configure only the environment variable names:

```toml
[channels.telegram]
token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env = "TELEGRAM_CHAT_ID"
```

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to your own values in the
process environment or `$GATEWAY_HOME/.env`. With those credentials, the
following command sends a real Telegram notice. Use the offline demo below
to inspect capabilities without sending a message:

```bash
tool-gateway --home "$GATEWAY_HOME" deliver \
  --from build --about release-42 --class event \
  --title "Build complete" \
  --line "tests: 174 passed" \
  --line "artifact: dist/package.whl"
```

Exit codes are part of the CLI contract:

- `0`: sent, deduplicated, deferred, queued for retry, or otherwise successful.
- `1`: permanent failure, invalid input, invalid configuration, or credentials.
- `75`: reserved shared-contract quota status.
- `111`: transient failure that could not be persisted for later retry.

## Demo

Capability discovery is fully offline and does not need a data directory:

```bash
tool-gateway capabilities --json
```

A loopback webhook demo uses two terminals. Start the server with the quick-start
configuration, then run:

```bash
curl -i \
  -H "X-Gateway-Token: ${GATEWAY_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"event":"example"}' \
  http://127.0.0.1:8787/hook/example
```

The gateway returns `202 Accepted` before waiting for the configured command.
The example command checks that the payload file exists and exits successfully.
The request and eventual command exit are recorded separately in the external
data directory; inspect `log/dispatch.jsonl` for `"exit": 0`.

## Limitations

- Telegram ingress treats the configured chat ID as the sender allow-list. A
  group chat shares one ID, so any group member can submit commands. Use a
  private operator chat unless you add a per-user filter.
- Webhook authentication uses one configured bearer-like header token. Provider
  HMAC signature schemes are not implemented.
- Telegram long polling needs a dedicated bot token. A second `getUpdates`
  consumer can steal updates.
- One webhook process returns 202 immediately and does not supervise the
  business workflow after spawn. Long-running work belongs in a durable tool or
  scheduler.
- Audit logs, payload files, queues, and receipts do not rotate automatically.
- Live-message closure, multi-project addressing, and long-running stability are
  covered hermetically but still require operator-specific live acceptance.
- The Linear configuration section is a validated placeholder; no Linear channel
  implementation is included.
- Tests contain no retained Cyrillic fixtures. Runtime parsing still accepts the
  legacy two-letter Russian weekday aliases and common approval replies through
  Unicode-escaped compatibility literals.

## Data and credential boundary

All mutable data is rooted at the explicit `--home` path:

```text
<home>/gateway.toml
<home>/.env
<home>/log/{inbound,dispatch,delivery}.jsonl
<home>/payloads/
<home>/state/{queue,live,event-receipts}/
<home>/templates/
```

No data path defaults to the repository or to a particular user's home. The
shell variable in the quick start is only an example chosen by the operator.

Configuration stores credential environment variable names, never credential
values. Process environment values override `<home>/.env`. Webhook payloads are
passed by file rather than argv, target commands receive only explicitly allowed
environment variables, and doctor reports credential presence as booleans.

Do not commit `.env` files, logs, payloads, state, receipts, or real message
content. The repository ignore rules cover these local artifacts.

## Tests

The full suite is hermetic. HTTP clients, external targets, and session-target
processes are injected except for loopback webhook tests and one local process-
group cleanup test using `/bin/sh` and `sleep`. No external network or real
credentials are required.

```bash
python3 -m pip install 'pytest>=8'
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

## License

MIT. Copyright (c) 2026 abetor. See [LICENSE](LICENSE).
