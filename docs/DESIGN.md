# Design

## Scope and dependency direction

`tool-gateway` is a thin boundary around external channels. It authenticates,
normalizes, routes, invokes configured processes, and records outcomes. It does
not contain domain workflow logic and does not become a Python dependency of the
tools it serves.

The direction is deliberate:

```text
gateway -> tool CLI or argv target
tool -> gateway CLI for outbound delivery
```

Calls cross process boundaries through documented CLI and JSON contracts. The
core never imports concrete channel implementations; the CLI imports them to
populate the registry.

## Configuration and data root

Every stateful command requires `--home`. There is no default based on cwd or a
specific user directory. `<home>/gateway.toml` has channel sections and route,
project, target, notification, and snapshot registries.

Configuration is fail-closed:

- Unknown top-level sections, nested fields, channels, and targets are errors.
- Webhook startup refuses missing or shorter-than-16-character tokens before
  binding a socket.
- Telegram watch refuses a configuration with no project destination.
- Session replies refuse ambiguous project-local IDs.
- Generic targets reject secret-looking argv values and receive only explicitly
  selected environment variables.

Configuration contains credential environment variable names, not values.
`<home>/.env` is optional and never overrides an already exported process
variable.

## Core

`tool_gateway/core/config.py` parses and validates one configuration file.
Channel configuration can be absent; `None` means disabled. Telegram delivery
can be configured without watch projects or a session target.

`tool_gateway/core/channel.py` defines normalized inbound events, the channel
contract, and a registry. A channel owns platform-specific polling, sender
filtering, message sending, editing, and health checks.

`tool_gateway/core/log.py` writes three append-only JSONL streams:

- `inbound`: accepted inputs and explicit refusals.
- `dispatch`: invoked commands, targets, and exit outcomes.
- `delivery`: sent, edited, deduplicated, deferred, queued, dropped, and refused
  messages with reasons.

The shared exit-code contract is `0` success, `75` quota, `111` transient, and
`1` permanent failure.

## Telegram channel and routing

The Telegram channel long-polls `getUpdates`, stores one offset under
`<home>/state`, and drops foreign chat IDs before emitting normalized events.
It classifies only established 401, 403, and 404 responses as permanent. Unknown
codes and transport failures stay transient so a new platform response does not
silently lose a notice.

Routing supports new tasks, `#N` session replies, `?M` question answers, project
prefixes, and correlation from the first line of a replied-to notification.
Session and message IDs are project-local, so replies fail closed when multiple
projects make an address ambiguous. Generic event targets accept only new tasks;
session reply syntax is rejected before invocation.

Each project has one queue and worker thread. Turns are serial within a project
and parallel across projects. Accepted but unfinished routes are journaled. A
restart reports leftovers without blind replay because the previous invocation
may have produced partial effects.

## Webhook channel

The webhook server binds to `127.0.0.1` by default. It compares tokens with
`hmac.compare_digest`, refuses unauthorized or unknown routes before spawn, and
places the request body in `<home>/payloads`. The command receives the payload
path through `GATEWAY_PAYLOAD` and the route through `GATEWAY_ROUTE`; payload
bytes never enter argv.

The server returns 202 immediately. A reaper thread later records the process
exit, separating ingress acknowledgment from workflow completion.

## Notification delivery

Plain text and legacy structured flags normalize into the version 2 envelope:

```text
from / about / class / title / lines / name / action / urgency / dedupe / live
```

Rendering uses per-class Telegram HTML templates. User fields are escaped and
bounded before transport. An operator may override a class template under
`<home>/templates/<class>.txt`.

Delivery state is protected by a file lock across concurrent CLI processes:

- Deduplication hashes stable envelope material and uses a per-class window.
- Availability grids defer configured classes and states.
- Transient platform failures enter a retry queue.
- `drain` sends eligible queue entries and logs expired or corrupt entries.
- A live envelope updates one message per `(from, about)` with edit pacing.
- A terminal event closes the previous live message; acknowledgements and status
  snapshots leave it open.

A delivery returns 0 after durable queueing because the gateway owns the retry.
It returns 111 only when a transient failure cannot be queued.

## Generic event targets

Configured targets are argv lists executed without a shell. One bounded,
canonical JSON event is written to stdin. The target returns one bounded JSON
receipt containing matching event identity, dedupe key, status, and effect hash.

Receipts live under `<home>/state/event-receipts`. A delivered receipt suppresses
replay, while failed and retryable receipts can recover on a later invocation.
Reusing an event ID with different bytes is a conflict. Parent process and pipe
drain share one monotonic deadline, preventing descendants that inherit stdout
or stderr from extending the timeout indefinitely.

Target stdout and stderr are untrusted. Invalid or oversized output becomes a
generic bounded diagnostic and is not copied into machine responses or Telegram
messages.

## Snapshots

Snapshot sources are configured commands returning JSON with a `snapshot`
object. The gateway merges flow workers and queues, optionally correlates them
with scheduler jobs by process group, and renders one status envelope. Malformed
source fields degrade into visible errors rather than crashing the whole
snapshot. When there are no workers and no source errors, the command records an
idle decision and sends nothing.

## Security boundaries

- The Telegram chat ID is an allow-list, not user authentication inside a group.
- Webhook tokens remain outside repository files and are never printed.
- External payload bytes use files rather than argv.
- Generic targets launch without a shell and receive an environment allow-list.
- Receipt and event identifiers are content-linked and validated before effects.
- Diagnostics are bounded and scrubbed from machine-facing output.
- Timeouts terminate a complete process group where descendant execution is
  possible.
- All logs, payloads, queues, templates, and receipts live under explicit home.

## Tradeoffs and non-goals

- This is not a workflow engine. A route starts another process and records the
  result; durable business execution belongs elsewhere.
- This is not a data store. Mutable data lives outside the repository and needs
  operator-managed retention and rotation.
- Webhooks use a gateway token, not provider-specific signature verification.
- Telegram groups require a future per-user allow-list before they are safe for
  privileged commands.
- The Linear section is a schema placeholder, not an implemented channel.
- Shared agent adapters are locally owned compatibility code and are not imported
  from sibling repositories.
