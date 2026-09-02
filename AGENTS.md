# AGENTS.md - tool-gateway

`tool-gateway` is a thin process boundary for authenticated ingress, outbound
delivery, routing, and audit logging. It may invoke configured tools through
CLI contracts, but it does not own their workflow logic and they must not
import its Python package. The repository contains code and synthetic fixtures
only. Configuration, credentials, payloads, logs, queues, templates, state,
and receipts belong under an explicit external home directory.

## Read in this order

1. `README.md` - scope, setup, data and credential boundary, CLI examples,
   limitations, and tests.
2. `docs/DESIGN.md` - configuration, routing, delivery, event, receipt, and
   security contracts.
3. `tests/` - executable behavior. Use `tests/_helpers.py` and
   `tests/conftest.py` for fixture conventions.

## Core invariants

- Keep the gateway thin: authenticate, normalize, route, invoke, deliver, and
  record. Domain workflow and business transformations stay in target tools.
- Cross tool boundaries through CLI processes or configured argv targets, not
  Python imports. Generic targets run without a shell.
- Configuration is fail-closed. Unknown fields, channels, routes, ambiguous
  addresses, invalid receipts, and missing credentials must not trigger work.
- External input and subprocess output are untrusted, bounded data. Never copy
  raw failures or secrets into machine responses or outbound messages.
- Accepted work must remain observable. Append audit outcomes and preserve
  replay-safe receipts instead of silently replaying an uncertain effect.
- Timeouts cover descendant processes where applicable, not only the immediate
  child.

## Working rules

- Before claiming a change passes, run exactly:

```bash
python3 -m pytest -q -p no:cacheprovider
```

- Keep tests hermetic. Inject clients and targets, use loopback-only fixtures,
  and never contact an external network, live bot, real webhook, or operator
  account. Test data stays inside temporary directories.
- Credential values for Telegram and webhooks stay outside the repository.
  Configuration stores environment variable names only; values come from the
  process environment or the external home file described in `README.md`.
- Every stateful command requires an explicit external home. Do not add a
  repository-relative or user-specific fallback. Mutable files never belong in
  source control.
- Pass webhook bodies by file and target events by bounded standard input.
  Never place payload bytes or credential values in argv.
- Keep source, tests, docstrings, CLI output, and documentation in English.
- Add a regression test before fixing an incident. Cover both the accepted
  path and the fail-closed path for routing or authentication changes.
- Keep modules aligned with the existing boundaries in `tool_gateway/core/`
  and `tool_gateway/ext/`. Prefer standard-library code and small explicit
  adapters over new framework layers.
- Commits use an imperative English subject, one logical change, no generated
  state or secrets, and plain ASCII punctuation.

## Contract changes

Treat CLI arguments and exit codes, the configuration schema, route matching,
the versioned notice envelope, event and receipt JSON, audit JSONL records,
home-directory layout, deduplication, retry, and acknowledgment semantics as
public contracts. A contract change must update `README.md` and
`docs/DESIGN.md` and add or adjust tests in the same change. Version machine
formats instead of silently changing their shape or meaning.
