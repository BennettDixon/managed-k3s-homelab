# gateway (manifests)

Model gateway of the personal cloud: an OpenAI-compatible
`POST /v1/chat/completions` (+ `GET /v1/models`, the `/ledger/*` read
endpoints) at `http://gateway/v1/…` (tailnet-only via the tailscale
operator, caller-token gated) that routes every model call of the house onto
a lane — in v1 the metered Anthropic API only; the subscription lane is
designed and deferred — under a per-project budget ledger in one SQLite
file on `gateway-data` (local-path PVC — enforcement state, NOT a cache).
Every request carries a caller token, a project and a budget cap; a missing
cap is an error, as in jobs-mcp. Models, prices, projects, caps and callers
live in `registry.yaml` (configMapGenerator: a change hash-rolls the
Deployment; CI parses it under the service's admission rules against the
deployment's `MAX_REQUEST_CAP_USD`). Depends on: the tailscale operator
(exposure as `gateway`), external-secrets (the caller-token JSON map, the
Anthropic key, the Harbor pull robot — three ExternalSecrets in this base;
their SM ARNs are in the ESO reader policy), the Harbor `gateway` project for
image pulls, kube-prometheus-stack's CRDs (ServiceMonitor + PrometheusRule,
`release` label). jobs-mcp executors are its callers from slice 3; NanoClaw
never is.
Prod-only: this base joins `apps/homelab-prod` and must never be added to
`apps/development`. No manifest here may contain a literal `${…}` (Flux
postBuild envsubst runs over apps/homelab-prod). Source: `services/gateway/`.
Spec: `docs/specs/gateway.md` (approved 2026-09-09). Operator runbook with
the MERGE GATE, rotation and the alert table: `docs/runbooks/gateway.md`.
