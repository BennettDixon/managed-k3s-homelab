# Runbook: knowledge-mcp

Operator-facing steps only — semantics (tools, caller classes, chunking,
freshness, alerts) live in the spec: `docs/specs/knowledge-mcp.md`.
Manifests: `apps/base/knowledge-mcp/`. Source: `services/knowledge-mcp/`.
Endpoint: `http://knowledge-mcp/mcp` (tailnet-only; one bearer per caller id).

## MERGE GATE (slice 2 — manifests) — order is load-bearing

The apps chain uses `wait: true` + `dependsOn`, so ONE unready knowledge-mcp
object (an ExternalSecret that cannot sync, a pod that cannot pull its image,
a registry that fails readiness) freezes reconciliation cluster-wide. Do NOT
merge the manifests PR until ALL of these exist:

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
   grep registry_parse_failed` = gate 3.
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
window either way; a stale executor fails loudly with `E_UNAUTHORIZED` and
`KnowledgeIndexStale` surfaces it within two days. Adding a caller = a new
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
3. Nothing ran at all: the jobs-mcp / n8n side (slice 3 section, below). A
   manual operator `reingest` is always safe — idempotent, sha-keyed.
4. GitHub unauthenticated API budget is 60 requests/hour per source IP; one
   reingest spends one tree call (raw fetches are not API calls). A 403 on
   the tree endpoint means something else on the same egress IP is spending
   that budget.

## Rebuild from scratch (PVC lost)

Nothing to restore — the index is a cache. A fresh PVC boots empty; run the
first-reingest step. Corpus #1 rebuilds from GitHub in seconds.

## Named seams (not built)

Litestream read-replica on edgepve (spec §11-S5), edge serving proper (S6),
vectors/hybrid (S1), NanoClaw onboarding (S7). None change these steps until
they land.
