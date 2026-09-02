# Runbook: knowledge-mcp

Operator-facing steps only — semantics (tools, caller classes, chunking,
freshness, alerts) live in the spec: `docs/specs/knowledge-mcp.md`.
Manifests: `apps/base/knowledge-mcp/`. Source: `services/knowledge-mcp/`.
Endpoint: `http://knowledge-mcp/mcp` (tailnet-only; one bearer per caller id).

## MERGE GATE (slice 2 — manifests) — order is load-bearing

The apps chain uses `wait: true` + `dependsOn`, so ONE unready knowledge-mcp
object (an ExternalSecret that cannot sync, a pod that cannot pull its image,
a registry that fails readiness) leaves the `apps` Kustomization NotReady on
a ~7 min retry cycle for as long as the gate is missed: everything else
under apps/homelab-prod slows to that cadence and `infra-network`, which
depends on it, waits. Do NOT merge the manifests PR until ALL of these exist:

1. **The two AWS SM entries — terraform-managed** (`terraform/main.tf`): set
   the four `knowledge_*` values in `terraform.tfvars` (template:
   `terraform.tfvars.empty`), then a **TARGETED** apply — full applies stay
   forbidden while the Lightsail proxy drift is parked (STATUS):
   ```bash
   cd terraform && terraform plan \
     -target=module.knowledge_mcp_caller_tokens_secret \
     -target=module.knowledge_harbor_docker_pull_secret
   ```
   Read the plan — 4 to add, 0 to change, 0 to destroy — then apply the same
   targets. Value sources:
   - `knowledge_mcp_operator_token`, `knowledge_mcp_n8n_reingest_token` —
     generated (`openssl rand -hex 32`), ≥16 chars, must differ (the pod
     refuses duplicate values at boot). The n8n-reingest value must ALSO live
     in the n8n LXC env as `KNOWLEDGE_REINGEST_TOKEN` (slice 3 gate).
   - `knowledge_harbor_docker_pull_username` / `_password` — the pull-only
     robot from step 2 (full `robot$knowledge+knowledge-pull` name).
2. **Harbor**: private project `knowledge`; robot `knowledge-pull` scoped to
   `repository:pull` on it, never expiring; the docker-push user as
   maintainer (mirror of `jobs`). Then the image **pushed**:
   ```bash
   cd services/knowledge-mcp
   docker buildx build --builder desktop-linux --platform linux/amd64 \
     -t harbor.internal/knowledge/knowledge-mcp:<semver> --push .
   ```
   The tag must equal the one in `apps/base/knowledge-mcp/deployment.yaml`.
   Use the `docker`-driver builder: `docker-container` builders push from
   inside BuildKit, which does not trust the homelab root CA.
   Harbor's admin login is NOT the SM value any more (STATUS finding
   2026-09-02): create projects/robots in the UI as `bennett`. If scripting
   it instead, authenticate as the docker-push user with `curl -u docker-push`
   (password prompted) or `--netrc` — never a password on a command line —
   and demote the creator from project admin to maintainer afterwards. The
   robot secret is shown ONCE at creation: capture it straight into
   `terraform.tfvars`.
3. **Registry parseable**: `apps/base/knowledge-mcp/corpora.yaml` passes the
   service's admission rules. CI runs `eval/registry-manifest.test.ts`
   against the real file on every PR touching it; locally `npm run eval` in
   `services/knowledge-mcp`.

## Deploy / rollback

Image is built and pushed from the workbench; deploys are image-tag bumps in
`apps/base/knowledge-mcp/deployment.yaml`; registry (`corpora.yaml`) changes
hash-roll the Deployment (Recreate: seconds of downtime, never two writers).
Rollback = revert the tag commit — migrations are additive-only, the DB
reopens. A rollback across a chunker-version bump re-chunks every doc at
boot from stored content (no network); the startup probe allows 2 minutes.
If boot dies mid-re-chunk (the one write on a boot path — a full node disk
is the realistic cause) the pod crash-loops rather than degrading: free
disk; each doc's re-chunk is its own transaction, so progress is kept.

## First run after merge

1. **Flux**: `kubectl -n flux-system get kustomization apps` Ready at the
   merge commit; `kubectl -n knowledge-mcp get externalsecret` → both
   `SecretSynced`; `kubectl -n knowledge-mcp get pods` → `1/1 Running`.
   Diagnose from kubectl, not the tailnet — a NotReady pod has no Service
   endpoints, so `http://knowledge-mcp` simply refuses connections:
   `ImagePullBackOff` = gate 2 (image not pushed) OR gate 1's pull-robot
   entry missing (kubelet pulls anonymously without the secret);
   `CreateContainerConfigError` = gate 1's token map missing;
   `0/1 Running` + `kubectl -n knowledge-mcp logs deploy/knowledge-mcp |
   grep registry_parse_failed` = gate 3. After fixing a gate-1 miss
   post-merge, ESO's error backoff can take ~17 min to retry — annotate the
   ExternalSecret with `force-sync=$(date +%s)` to shortcut it.
2. **Probes over the tailnet** (once Ready): `curl http://knowledge-mcp/healthz`
   and `/readyz` → `{"ok":true}`.
3. **First reingest** — the index starts EMPTY; search returns nothing until
   this runs. With the operator token exported as `KNOWLEDGE_MCP_TOKEN`
   (never on the command line of a shared shell history):
   ```bash
   # -f: a 401 prints "curl: (22) ... 401" instead of nothing
   mcp() { curl -sS -f -X POST http://knowledge-mcp/mcp \
     -H "Authorization: Bearer $KNOWLEDGE_MCP_TOKEN" \
     -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     -d "$1" | sed -n 's/^data: //p'; }
   mcp '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"reingest","arguments":{"corpus":"homelab-notes"}}}'
   mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"corpus":"homelab-notes","query":"tailscale gateway sysctl forwarding","k":3}}}'
   ```
   What prints is the JSON-RPC envelope; the tool result is the JSON string
   in `.result.content[0].text` (append `| jq -r '.result.content[0].text'`
   to unwrap it). Expect the reingest result `{"corpus":"homelab-notes",
   "source_ref":"<tree sha>","indexed":N,"unchanged":0,"tombstoned":0,
   "errors":[]}` and search hits carrying `path`, `heading_path`, `trust`,
   `source_commit`. A non-empty `errors` list = per-doc fetch failures;
   `index_as_of` does not advance until a clean sweep (spec §6) — fix,
   re-run (idempotent). Until the nightly `knowledge-reingest` job (slice 3)
   runs, `KnowledgeIndexStale` WILL fire 48 h after the last manual
   reingest — expected; re-run by hand or accept it.
4. **Metrics**: Prometheus `up{namespace="knowledge-mcp"}` = 1,
   `knowledge_docs{corpus="homelab-notes"}` > 0, rules present:
   `kubectl -n knowledge-mcp get prometheusrule`.

## Workbench MCP registration

See `mini/mcp-config.md`: `claude mcp add --transport http knowledge-mcp
http://knowledge-mcp/mcp --header 'Authorization: Bearer ${KNOWLEDGE_MCP_TOKEN}'`
with the OPERATOR token in the account's `~/.zshenv`. NanoClaw never
receives the operator token (SIGN-OFF 3: its own caller id, curated corpus).

## Rotation / adding a caller

Edit the value in `terraform.tfvars` → targeted apply → the ExternalSecret
refreshes within 1h (or annotate the ExternalSecret with
`force-sync=$(date +%s)`) → the pod reads the map at BOOT only:
`kubectl -n knowledge-mcp delete pod -l app=knowledge-mcp` (Recreate,
seconds; preferred over `rollout restart`, whose annotation Flux's SSA later
strips for a second bounce). Then update every holder of the old value:
workbench `~/.zshenv`, the n8n LXC env for `n8n-reingest`. There is a skew
window either way; a stale executor fails loudly (`knowledge-mcp returned
401 E_UNAUTHORIZED` in the job's error message) and `KnowledgeIndexStale`
surfaces it within two days. Adding a caller = a new
map key (terraform) + a `callers:` line in `corpora.yaml` — the PR is where
a human reads what they grant; a token whose id has no registry entry
authenticates nobody.

Harbor robot rotation: create a new robot in the UI, put its name/secret
into `terraform.tfvars` → targeted apply of
`module.knowledge_harbor_docker_pull_secret` → ExternalSecret refresh →
delete the old robot. Pulls only happen at pod (re)creation, so a stale
secret shows up as `ImagePullBackOff` on the next deploy, never mid-run.

## Reingest failing (`KnowledgeIndexStale`) checklist

1. Tree-level failures (GitHub API unreachable or 403, a truncated
   listing, the circuit breaker) are NOT in pod logs — they reach only the
   caller's tool result and `knowledge_ingest_errors_total{code}`: read the
   job's `status(id).error` / the n8n execution output, or re-run
   `reingest` by hand and read the error. A sweep that completed with
   per-doc failures does log `"evt":"reingest"` with its `errors` count
   (`kubectl -n knowledge-mcp logs deploy/knowledge-mcp | grep reingest`).
   The code names the class: `E_URI_UNREACHABLE` (GitHub raw/API
   unreachable from the pod, or a truncated tree listing),
   `E_DOC_TOO_LARGE` (a doc outgrew `max_doc_bytes` — raise it in the
   registry, ≤512KiB), `E_INTERNAL` (disk full — the node, not the claim:
   see `JobsPvcDiskFilling`).
2. `E_UNSUPPORTED: tree listing matched no documents while the corpus has
   live docs` = the circuit breaker: registry prefixes and the repo tree
   disagree (a renamed directory). Fix the registry; nothing was tombstoned.
3. Nothing ran at all: `status(id)` of the last `knowledge-reingest` job.
   `state: failed` ⇒ `error.code` is always `retries_exhausted`; the
   diagnosis is in `error.message`: `webhook returned 404` = workflow not
   imported/active; `webhook returned 500` = the n8n execution errored
   (read its log); `knowledge-mcp returned 401 E_UNAUTHORIZED` = token skew
   between the SM map and the n8n env; `N document(s) failed: <code path>`
   = per-doc failures (item 1). `state: queued` with `attempts > 0` = still
   retrying (`error` holds the last attempt's own code). n8n unreachable
   fails nothing: the job sits `queued` and `jobs_bridge_up` drops to 0
   (jobs runbook "Bridge down"). Then the nightly trigger's own n8n
   execution history. A manual operator `reingest` is always safe —
   idempotent, sha-keyed.
4. GitHub unauthenticated API budget is 60 requests/hour per source IP; one
   reingest spends one tree call (raw fetches are not API calls). A 403 on
   the tree endpoint means something else on the same egress IP is spending
   that budget.

## MERGE GATE (slice 3 — `knowledge-reingest` task_type)

The registry change hash-rolls jobs-mcp (Recreate, seconds of downtime, no
queue loss); jobs-mcp's non-fatal startup check then expects the workflow to
exist and be active. Do NOT merge the task_type PR until ALL of:

1. **Slice 2 is live**: `http://knowledge-mcp/readyz` → `{"ok":true}` and
   the first operator reingest has succeeded (First run, above).
2. **`n8n-reingest` token in BOTH places**: the SM map (slice 2's targeted
   apply) AND the n8n LXC env as `KNOWLEDGE_REINGEST_TOKEN` in
   `/etc/n8n/n8n.env`, followed by `systemctl restart n8n` (the alert
   receiver and the jobs executors blink for ~10 s).
3. **Workflow `knowledge-reingest` imported and ACTIVE** on n8n from
   `n8n/knowledge-reingest.json` — import through the workbench
   `N8N_API_KEY`; the cluster-synced jobs-mcp key is read-only.
4. **Executor proven directly** (bypassing the queue, with the webhook
   secret exported as `JOBS_WEBHOOK_SECRET`):
   ```bash
   curl -sS -X POST http://n8n:5678/webhook/jobs/knowledge-reingest \
     -H "X-Jobs-Webhook-Secret: $JOBS_WEBHOOK_SECRET" -H 'Content-Type: application/json' \
     -d '{"job_id":"manual-test","attempt":1,"payload":{"corpus":"homelab-notes"}}'
   # expect {"ok":true,"result":{"corpus":"homelab-notes","source_ref":"<sha>",...},"artifacts":[],"spent_usd":0}
   ```

Accepted risk (same trust domain as the jobs webhooks and the alerts
bearer): `X-Jobs-Webhook-Secret` is persisted verbatim in every
`knowledge-reingest` execution, authorized or forged — webhook items keep
raw headers, and n8n's header redaction covers only `authorization` /
`cookie` and is not active — readable by any n8n UI/API identity.
`KNOWLEDGE_REINGEST_TOKEN` (and `JOBS_MCP_BEARER_TOKEN`, if ever armed)
never enter execution data — the HTTP node stores only the response and
redacts `Authorization` in failed-request context — but any workflow author
can print `$env`. One trust domain, as `proxmox/n8n.md` states. Gate step 4
is also the first live proof of the n8n-node → knowledge-mcp path: a
connection error there is an ACL question, not a token one.

After merge, the end-to-end proof from any jobs-mcp client:
`enqueue {task_type: "knowledge-reingest", payload: {corpus: "homelab-notes"},
budget_cap: 0, artifacts_out: []}` → `status(id)` reaches `succeeded` with
`result.source_ref` = the current `main` commit → knowledge-mcp
`list_corpora` shows `index_as_of.commit` equal to it.

## Scheduled freshness (nightly)

`n8n/knowledge-reingest-nightly.json` enqueues one `knowledge-reingest` job
per day at 03:30 instance time (day-keyed `idempotency_key`: a re-fired
trigger is a replay, never a second run; the executor is idempotent anyway).
It needs the jobs-mcp bearer in the n8n env as `JOBS_MCP_BEARER_TOKEN`.
That hands n8n — and every workflow author on it, since Code nodes read
`$env` — the WHOLE jobs-mcp v1 tool surface: enqueue any task_type (the cap
is per job only), read every job's result/error, cancel queued jobs. Nil
today (n8n already is the only executor and holds `JOBS_WEBHOOK_SECRET`);
real the day a worker or gateway executor lands. The workflow is therefore
imported INACTIVE; arming it is an explicit operator decision. Prefer, in
order: (1) a Schedule trigger calling knowledge-mcp `reingest` directly with
the reingest-bot token n8n already holds — zero new secrets; it loses only
the job row (a spec §6 deviation, so it needs the operator's sign-off;
failure visibility is unchanged: `KnowledgeIndexStale` + n8n execution
history); (2) once jobs-mcp has per-caller tokens (its NanoClaw retrofit),
an `n8n-nightly` caller allowed only `enqueue knowledge-reingest`; (3) arm
this workflow as-is: add `JOBS_MCP_BEARER_TOKEN` to `/etc/n8n/n8n.env`,
restart n8n, execute the workflow ONCE from the editor and confirm it prints
a `job_id` (the trigger has never run — its enqueue path is verified only by
review harness), then activate. A CronJob holding the operator bearer is the
same credential in a second namespace, not an improvement. Either way,
manual enqueue after doc-heavy merges is always available from any jobs-mcp
client (the same envelope as above). Failure visibility: an unsuccessful enqueue errors the
n8n execution; a reingest that keeps failing surfaces as
`KnowledgeIndexStale` within two days.

## Rebuild from scratch (PVC lost)

Nothing to restore — the index is a cache. A fresh PVC boots empty; run the
first-reingest step. Corpus #1 rebuilds from GitHub in seconds.

## Named seams (not built)

Litestream read-replica on edgepve (spec §11-S5), edge serving proper (S6),
vectors/hybrid (S1), NanoClaw onboarding (S7). None change these steps until
they land.
