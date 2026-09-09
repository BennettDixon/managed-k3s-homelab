# gateway

> **Alpha, and mostly AI-written.** This service was spec'd, implemented, and
> adversarially reviewed by Claude agent sessions under human direction, and
> runs here as a personal-infrastructure testbed while its operator kicks the
> tires. Expect sharp edges; interfaces and internals may change without
> notice. A polished, properly packaged open-source version will likely be a
> later rewrite — don't build on this one.

`gateway` is the model gateway of the personal cloud: an OpenAI-compatible
`POST /v1/chat/completions` at `http://gateway/v1/…` (tailnet-only,
caller-token gated) that routes every model call of the house onto a lane —
in v1 the metered Anthropic API; the operator's Claude subscription, executed
by the unmodified `claude` binary on `worker-01` through a pull protocol whose
credential never leaves that box, is designed and deferred — under a
per-project budget ledger in one SQLite file on a PVC. Every request carries a
caller token, a project and a budget cap; a missing cap is an error, exactly
as in jobs-mcp. Depends on: tailscale-operator (exposure), external-secrets
(caller-token map, metered key, Harbor robot), Harbor (image),
kube-prometheus-stack CRDs, and, only when the deferred subscription lane is
built, `worker-01`. jobs-mcp executors and future workers are its callers;
NanoClaw never is. Spec: `docs/specs/gateway.md` (approved 2026-09-09).

The first Python service of the house (spec §12): `python:3.12-slim`, `uv`,
`ruff` + `mypy --strict` + `pytest` (+ `hypothesis` for the §14 property
tests), `pydantic`, `prometheus_client`, the official `anthropic` SDK, stdlib
`sqlite3`, a single-process ASGI server. The six house idioms are ported here
once — loud env validation (`config.py`), the caller-map parse + constant-time
auth (`config.py`, `auth.py`), the `/healthz`–`/readyz` split (`http.py`),
zero-filled series + absent guard (`metrics.py`), additive migrations
(`db.py`), one-line JSON logs (`jsonlog.py`) — and become the precedent the
next Python service copies.

## Slice 1 (this directory)

Code + tests + CI only — no manifests, no secrets, nothing reconciles or
spends. The upstream client is used only behind a reservation, and the test
suite and local smoke run against a fake that reaches nothing.

```bash
uv sync && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
uv run gateway-smoke-local   # boots against a stub upstream: one completion at cap $0.01, the ledger row, a 402 for cap 0
```

Money discipline (spec §1): integer micro-USD everywhere (`money.py`); one
`sqlite3` connection opened with `isolation_level=None`, a process-wide mutex
around every money transition, explicit `BEGIN IMMEDIATE`, and a conditional
compare-and-add on the materialized `scope_totals` table as the enforcement
statement (`ledger.py`). Reservations are swept at boot before the listener
opens; unknown spend settles at the reservation, never at zero.

`registry.example.yaml` is the spec §4 example; `apps/base/gateway/registry.yaml`
(slice 2) is parsed under the same admission rules in CI.
