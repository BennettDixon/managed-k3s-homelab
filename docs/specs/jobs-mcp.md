# jobs-mcp — v1 specification

**Status: APPROVED 2026-08-31 — all four sign-off decisions (storage, delivery semantics, auth, v1 scope) accepted as specified. Implementation may begin at slice 1 (§12).**

**README paragraph (repo convention):** `jobs-mcp` is the task-queue MCP service of the personal cloud. It accepts job envelopes over MCP (`enqueue`, `status`, `artifacts`, plus `cancel`), persists them in SQLite on a local PVC, and dispatches them to deterministic executors — n8n workflows reached over the tailnet. Depends on: tailscale-operator (exposure + n8n egress), external-secrets (bearer token, n8n API key, webhook secret), Harbor (image). Job records live on its PVC; artifact payloads live on the bulk NAS at `truenas-bulk-52tb:/mnt/BulkPoolZ2/artifacts/jobs/`. Agentic executors and the model gateway plug in later behind the same interface.

Design stance: the smallest thing that is restart-safe end to end. One process, one file-backed store, one direction of HTTP calls, and one failure model — **power dies mid-write, at any moment, on any box**. Every transition recovers by process restart with no operator action. Public-repo safe: tailnet MagicDNS names only.

## 1. Backing store

SQLite, single file `/data/jobs.db`, `PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;`, on PVC `jobs-mcp-data` (1Gi, `local-path`, RWO). Every state transition is one committed transaction: once `enqueue` returns an id, the job survives an immediate power cut. No Redis/NATS/Postgres — on a single-node cluster they add operational surface and zero durability. The single-writer constraint is enforced structurally (§7), not by convention.

```sql
CREATE TABLE jobs (
  id              TEXT PRIMARY KEY,   -- ULID: time-sortable, safe as NAS dir names
  task_type       TEXT NOT NULL,
  payload         TEXT NOT NULL,      -- JSON, <= 64 KiB
  budget_cap_usd  REAL NOT NULL,
  priority        INTEGER NOT NULL,
  artifacts_out   TEXT NOT NULL,      -- JSON array of relative paths
  idempotency_key TEXT UNIQUE,        -- permanent, no TTL
  envelope_hash   TEXT,               -- for idempotency conflict detection
  state           TEXT NOT NULL,      -- queued|running|succeeded|failed|canceled
  attempts        INTEGER NOT NULL DEFAULT 0,
  max_attempts    INTEGER NOT NULL,
  not_before      INTEGER,            -- backoff gate, unix ms
  error           TEXT,               -- JSON {code, message, retryable}
  result          TEXT,               -- inline result JSON, <= 64 KiB
  spent_usd       REAL,               -- null until the gateway ledger exists
  artifacts       TEXT,               -- JSON manifest reported by executor
  enqueued_at     INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER
);
CREATE INDEX idx_dispatch ON jobs(state, priority, id);
```

Schema migrations run at boot in a transaction and are additive-only within a minor version, so a one-step image-tag rollback reopens the DB safely. DB backup and cross-site replication (e.g. Litestream) are deferred; the seam is that all state lives in one file. Manifest comment: *single-site by design; job records do not survive loss of the node — re-enqueue is the recovery model; artifacts on the NAS persist.*

## 2. Transport, exposure, auth

- **Transport:** MCP Streamable HTTP (stateless) at `POST /mcp`, container port 8080. Node LTS + official MCP TypeScript SDK. Plain HTTP — the tailnet (WireGuard) is the transport security; there is no non-tailnet path.
- **Exposure:** ClusterIP Service annotated `tailscale.com/expose: "true"` and `tailscale.com/hostname: "jobs-mcp"`; clients use `http://jobs-mcp/mcp`. Note: personal-site uses only `expose` today; the `hostname` annotation is a valid operator feature at the deployed chart version — this is deliberate, not drift.
- **Auth:** two layers. (1) Tailnet reachability — ACLs are the perimeter and are appliance-tier; this spec neither requires nor touches them. (2) One static bearer token on every MCP request, checked against Secrets Manager key `k3s_jobs_mcp_bearer_token` synced by an ExternalSecret (harbor-admin-password pattern). Rotation = rotate in AWS SM, wait for refresh, pod restart.
- **NanoClaw:** is **not** a v1 caller and never receives this token. Its onboarding is a named prerequisite task: per-caller tokens (a small change to the auth check), the registry `frontend_allowed` fence (§5, shipped now), and its tagged-identity ACL grant (explicit appliance-tier operator task). Until all three exist, NanoClaw cannot reach jobs-mcp — confinement is never policy-by-hope.

## 3. Envelope and validation

```json
{ "v": 1, "task_type": "smoke-heartbeat", "payload": { },
  "budget_cap": 0, "priority": 5, "artifacts_out": ["report.json"],
  "idempotency_key": "optional-client-string" }
```

Validation runs entirely at enqueue; a rejected envelope creates no row. Unknown top-level fields are rejected (`E_SCHEMA`).

- **v** — absent ⇒ 1; any other value ⇒ `E_ENVELOPE_VERSION`. v2 changes are additive and gated here.
- **task_type** — required; `^[a-z0-9][a-z0-9-]{1,62}$`; must exist in the registry, else `E_TASK_TYPE_UNKNOWN`. `payload` (required object, ≤ 64 KiB) is then validated against the entry's `payload_schema` when present (`E_PAYLOAD_INVALID`); otherwise opaque, passed verbatim.
- **budget_cap** — **required, scalar USD number, no default, ever.** Finite, `>= 0`, `<= MAX_BUDGET_CAP_USD` (env, default 25.00 — fat-finger guard). `0` is legal and means *no model spend permitted* — correct for deterministic jobs. Missing/null/wrong shape ⇒ `E_BUDGET_CAP_MISSING` / `E_BUDGET_CAP_INVALID`. jobs-mcp validates, records, and propagates; enforcement is the future gateway's job.
- **priority** — optional int 0–9, 0 most urgent, default 5. Dispatch order strictly `(priority ASC, id ASC)` (ULID = FIFO within band). No preemption, no aging: one user, low volume.
- **artifacts_out** — required array (may be empty), ≤ 32 entries matching `^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$` **plus** an explicit check: no `..` segment, no leading `/`. This is the job's declared output contract (§6).
- **idempotency_key** — optional, 8–128 chars, permanently UNIQUE (no TTL — an expiring key double-runs). Re-enqueue with the same key and same envelope hash returns the existing id with `idempotent_replay: true`; same key, different hash ⇒ `E_CONFLICT_IDEMPOTENCY`. (Becomes per-caller when caller classes exist.)

**Error taxonomy** — every tool error is `{code, message, retryable}`: `E_UNAUTHORIZED`, `E_SCHEMA`, `E_ENVELOPE_VERSION`, `E_TASK_TYPE_UNKNOWN`, `E_PAYLOAD_INVALID`, `E_BUDGET_CAP_MISSING`, `E_BUDGET_CAP_INVALID`, `E_CONFLICT_IDEMPOTENCY`, `E_NOT_FOUND`, `E_NOT_CANCELABLE`, `E_INTERNAL` (retryable; details logged, never leaked).

## 4. Task-type registry

Repo-owned ConfigMap at `apps/base/jobs-mcp/registry.yaml`, emitted via **`configMapGenerator`** so any registry change hash-rolls the Recreate Deployment — a merged registry PR takes effect declaratively, with no hot-reload code path and no silent staleness. Parsed at startup; parse failure fails readiness.

```yaml
task_types:
  smoke-heartbeat:
    executor: n8n            # only value in v1; "worker" reserved (§9)
    webhook_path: jobs/smoke-heartbeat
    timeout_s: 300
    max_attempts: 3
    idempotent: true         # MANDATORY true for executor: n8n (registry admission)
    frontend_allowed: false  # NanoClaw fence; load-bearing when per-caller tokens land
    payload_schema: { type: object, additionalProperties: false }  # optional in general
```

Rules enforced at parse time: `executor: n8n` requires `idempotent: true`; `payload_schema` is **required** for any task_type whose workflow contains SSH/exec/command nodes (payload becomes attacker-influenced the day a front-end can enqueue). Governance: a new task_type is one PR carrying the registry entry **and** the canonical workflow export in `n8n/` (repo is source of truth per `n8n/README.md`); CI-checkable invariant: every `webhook_path` has a matching workflow JSON. No runtime registration API.

## 5. Lifecycle, delivery semantics, crash recovery

**States:** `queued → running → succeeded | failed | canceled`. Terminal rows are immutable. Every transition is one SQLite transaction.

**Dispatcher:** an in-process loop (concurrency `DISPATCH_CONCURRENCY=2`). For each claim it **commits `running` (started_at, attempts+1) before the HTTP call** — two-phase in effect: intent is durable before the side effect — then awaits the executor synchronously, bounded by the registry `timeout_s`.

**Delivery is at-least-once, full stop.** Exactly-once cannot be bought across a power cut between two machines, so it is not pretended. The corollary is a hard registry-admission requirement: every n8n executor workflow is **idempotent keyed on `job_id`** (writes keyed by job_id; the per-job artifact directory is overwritten on re-run). There is no at-most-once mode — it produced false terminal failures on every routine deploy, the most common operational event.

**Crash recovery:** with exactly one process and one writer, *any `running` row found at boot is by definition an orphan* — no leases, heartbeats, or clocks needed. Startup sweep: `attempts < max_attempts` ⇒ back to `queued` (attempt consumed, backoff `not_before = now + 30s·2^attempts`, cap 15 min, jittered); exhausted ⇒ `failed {code: "retries_exhausted"}`, noting the last execution may still have completed in n8n — harmless by the idempotency contract. Leases arrive only when a second claimant (the worker driver) exists; this paragraph is the documented reason they are deferred.

**SIGTERM / rollouts:** on SIGTERM the loop stops claiming, finishes or abandons in-flight awaits within `terminationGracePeriodSeconds` (30s), and exits. Anything severed is recovered by the boot sweep. A deploy during a running job is a designed non-event.

**Failure handling:** `ok:false`, non-2xx, network error, or timeout ⇒ attempt failed ⇒ retry-or-fail per `max_attempts`. Timeout means *abandon locally and mark the attempt failed* — the n8n public API has no stop endpoint, and the execution may still finish; idempotency makes that harmless. n8n unreachable ⇒ dispatch backs off exponentially (cap 2 min), sets `jobs_bridge_up 0`, and **nothing is failed because of bridge downtime**; enqueue keeps working.

**cancel(id):** `queued → canceled` atomically; any other state ⇒ `E_NOT_CANCELABLE`. Stopping a running n8n execution is not offered — pretending otherwise is theater; the timeout bounds the damage.

**status(id):** returns every field in every state (nulls where unknown), so clients never branch on shape: `{id, task_type, state, priority, attempts, max_attempts, budget_cap_usd, spent_usd: null, enqueued_at, started_at, finished_at, error, artifact_count, idempotent_replay}`. Unknown id ⇒ `E_NOT_FOUND`.

## 6. n8n deterministic-executor bridge

**Reachability (verification-gated):** pods cannot be assumed to resolve tailnet MagicDNS names (CoreDNS forwards to the node's upstream resolvers, not Tailscale split-DNS). **Pre-implementation check:** prove whether a pod can reach `http://n8n:5678`. If not — the expected case — the base ships an ExternalName Service `n8n` in namespace `jobs-mcp` annotated `tailscale.com/tailnet-fqdn`, which the operator rewires to an egress proxy; `N8N_BASE_URL=http://n8n.jobs-mcp.svc.cluster.local:5678`. The FQDN value is supplied as an overlay patch; **decided (operator, 2026-08-30): the ts.net tailnet FQDN is never committed to this public repo** — the patch file is gitignored, and its expected path gets a .gitignore entry when implementation lands. Stated assumption to verify (ACLs are appliance-tier): the tailnet ACLs permit `tag:k8s → n8n:5678`.

**Trigger:** synchronous `POST {N8N_BASE_URL}/webhook/jobs/<task_type>` with headers `X-Jobs-Webhook-Secret: <secret>` and body:

```json
{ "job_id": "01J…", "task_type": "…", "attempt": 1, "payload": { },
  "budget_cap_usd": 0, "artifacts_dir": "/mnt/BulkPoolZ2/artifacts/jobs/<task_type>/<job_id>/",
  "artifacts_out": ["report.json"] }
```

**Webhook auth is v1-mandatory:** every executor workflow begins with an IF node rejecting requests without the shared secret (Secrets Manager key `k3s_jobs_mcp_webhook_secret`, ExternalSecret into the pod; registry admission requires the check). Without it, any tailnet actor could POST a forged body directly to the webhook — bypassing envelope validation, the queue record, and `budget_cap` entirely; the future gateway would enforce a cap jobs-mcp never issued.

**Completion:** the workflow's final node is *Respond to Webhook* returning `{"ok": true, "result": {…}, "artifacts": [{"name","bytes","sha256"}], "spent_usd": 0}` or `{"ok": false, "error": {"code","message"}}`. `result` ≤ 64 KiB — anything bigger is an artifact. One HTTP round trip is the whole protocol: no callback endpoint, no execution polling, no adopt-or-requeue scanning of the executions API (the shakiest surface in any draft).

**n8n API key:** a dedicated key (never the workbench's), `k3s_jobs_mcp_n8n_api_key` → ExternalSecret → env `N8N_API_KEY`. Used **only** for a non-fatal startup check that each registry workflow exists and is active (`GET /api/v1/workflows`; n8n down must not block enqueue) and best-effort, non-load-bearing failure forensics. Documented blast radius: community n8n keys are full-access — workflow mutation equals code execution on the n8n LXC and read access to its stored credentials; pod compromise therefore reaches that far, which is why the key's use is minimal and why executor-side credentials stay out of this pod.

## 7. Artifacts

- **Layout:** `truenas-bulk-52tb:/mnt/BulkPoolZ2/artifacts/jobs/<task_type>/<job_id>/<name>` — tailnet name only; task_type grouping aids future pruning; ULIDs keep prefixes time-sorted.
- **Writers:** executors, never jobs-mcp. The scp-over-tailnet path is proven by the worker smoke test. **jobs-mcp holds zero NAS credentials and stays entirely off the data path** — it records only the manifest the executor reports. Retiring the smoke-era root-scp for a dedicated restricted NAS user (`svc-jobs`, key in n8n's credential store, executor-side) is the **named prerequisite task of the first bulk-writing task_type** — an appliance-tier NAS touch, never folded into this deploy. v1's only task_type writes no bulk artifacts.
- **Contract enforcement (hard):** every `artifacts_out` path must appear in the reported manifest, else the job fails `{code: "artifacts_missing"}`. Reported-but-undeclared files are recorded and flagged `undeclared: true` — not fatal.
- **artifacts(id):** listings, never content: `{id, state, artifacts_dir, artifacts: [{name, uri, bytes, sha256, undeclared?}]}` with scp-style `truenas-bulk-52tb:/…` URIs (no usernames embedded). Callers fetch over the tailnet themselves; content serving is knowledge-MCP territory.

## 8. Kubernetes shape and Flux wiring

No new Flux Kustomization objects — the existing `app-namespaces → app-secrets → apps` chain delivers everything in order:

```
apps/namespaces/jobs-mcp.yaml                      # Namespace jobs-mcp (per-app namespace scheme; follow harbor.yaml's shape, not personal-site.yaml's labels-outside-metadata drift)
apps/secrets/jobs-mcp-bearer-token.yaml            # ExternalSecret, ClusterSecretStore aws-secrets-manager
apps/secrets/jobs-mcp-n8n-api-key.yaml
apps/secrets/jobs-mcp-webhook-secret.yaml
apps/secrets/jobs-mcp-registry-harbor-docker-login.yaml  # dedicated robot, personal-site-registry pattern
apps/base/jobs-mcp/{kustomization,deployment,service,pvc,registry.yaml,servicemonitor}.yaml + README.md
apps/homelab-prod/kustomization.yaml               # + ../base/jobs-mcp
services/jobs-mcp/                                 # source + Dockerfile
```

- **Workload:** Deployment, `replicas: 1`, `strategy: Recreate` — with the RWO PVC this guarantees the old pod is gone before the new one opens the DB: upgrades and rollbacks can never produce two writers. Rollback = revert the image tag.
- **Image:** `harbor.internal/jobs/jobs-mcp:<semver>` (per-app Harbor project convention), built and pushed from the workbench like personal-site; deploys are image-tag bump commits.
- **Probes:** liveness `GET /healthz` — process up + DB file open, **never a write** (a full PVC must not become CrashLoopBackOff); readiness `GET /readyz` — DB write-ping, registry parsed, token loaded. **n8n reachability is in neither**: enqueue/status/artifacts must work while n8n is down; bridge health is a metric.
- **Resources:** requests `50m/128Mi`, limits `250m/256Mi`. PVC 1Gi with 80% usage alert.
- **Config:** env `N8N_BASE_URL`, `PORT=8080`, `DISPATCH_CONCURRENCY=2`, `MAX_BUDGET_CAP_USD=25`; secrets via the three ExternalSecrets. No IPs anywhere. When site labels land (build-order item 1), add `nodeSelector` per the label scheme.

## 9. Observability

- **Logs:** structured JSON to stdout, one line per enqueue and transition (`job_id, task_type, from, to, attempt, latency_ms, error`) — never payload contents. Shipped, not tailed.
- **Metrics:** `GET /metrics` + ServiceMonitor **labeled `release: kube-prometheus-stack`** (without it the default-valued stack silently ignores it): `jobs_state_count{state,task_type}`, `jobs_enqueued_total{task_type}`, `jobs_transitions_total{from,to}`, `jobs_dispatch_duration_seconds{task_type}`, `jobs_bridge_up`, `jobs_queue_oldest_age_seconds`, `jobs_db_bytes`. Starter alerts: `jobs_bridge_up == 0` for 10 min; PVC > 80%.

## 10. Failure matrix (what the design guarantees)

| Failure | Outcome | Mechanism |
|---|---|---|
| Power cut during enqueue | Job exists or doesn't; client retry with same idempotency_key is exact-once | Single transaction + permanent UNIQUE key |
| Crash/deploy mid-dispatch | Requeued at boot; duplicate execution harmless | running-before-HTTP + orphan sweep + idempotent-by-job_id |
| n8n unreachable | Enqueue works; dispatch freezes with backoff; nothing failed by downtime | Bridge backoff, readiness unaffected |
| Timeout / stuck workflow | Attempt failed locally; execution may still finish — harmless | Abandon-locally (no stop API), idempotency |
| Forged webhook call | Rejected by workflow | Shared-secret header, v1-mandatory |
| PVC full | Clean enqueue error, no corruption, no crash-loop | SQLite transactionality; liveness never writes; 80% alert |
| Upgrade/rollback | Never two writers; DB reopens | Recreate + additive-only migrations |

## 11. Seams and non-goals

**Agentic executors:** every executor sits behind an internal interface `dispatch(job) → completion report`; the registry `executor` field is the switch. `executor: worker` (v2) adds pull-based `claim`/`complete` endpoints gated by tagged tailnet identities, and introduces leases **then** — when a second claimant first exists. Envelope, tools, states, and artifact convention are untouched; that is the test this seam was designed against. **Model gateway:** `budget_cap` already flows enqueue → store → executor payload; when the gateway lands it enforces per `job_id` and `spent_usd` goes non-null — additive, no interface change. **Identity:** per-caller tokens + `frontend_allowed` become load-bearing at NanoClaw onboarding; Tailscale WhoIs can augment later.

**Out of scope v1:** spend enforcement; agentic executors; stopping running executions; artifact content serving, verification, retention, or pruning (destructive — explicit human-confirmed task); multi-replica HA and DB replication/backup (first v1.x task); cron/scheduling (n8n owns triggers); per-caller tokens and backpressure; job DAGs; the NVMe storage class.

## 12. Operator prerequisites and first slices

**Prerequisites (manual, before first reconcile):** Harbor project `jobs` + robot account + its AWS SM entry; AWS SM entries for bearer token, n8n API key (created in the n8n UI as a second key), webhook secret; pod→n8n reachability check (§6 — the FQDN-in-repo question is already decided: it stays out); ACL verification `tag:k8s → n8n:5678`; n8n execution-data retention (`saveDataOnSuccess`) pinned in the runbook; workflow activated via `POST /api/v1/workflows/{id}/activate`.

**Slices:** (1) `services/jobs-mcp/` — server, dispatcher, SQLite, tests; no manifests. (2) Manifests + secrets + egress Service, wired as §8. (3) `smoke-heartbeat` migrated from GET to the POST contract at `/webhook/jobs/smoke-heartbeat` (canonical export updated in `n8n/`), registry entry, end-to-end `enqueue → running → succeeded` from the workbench; runbook `docs/runbooks/jobs-mcp.md` updated.