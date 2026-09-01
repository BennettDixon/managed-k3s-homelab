# Runbook: jobs-mcp

Operator-facing notes for the task-queue MCP service. Spec:
`docs/specs/jobs-mcp.md`. Manifests: `apps/base/jobs-mcp/`. Source:
`services/jobs-mcp/`.

## One-time prerequisites (before first reconcile)

1. **AWS Secrets Manager entries** (region us-east-1, same account as the
   other `k3s_*` keys). Each is a JSON object with the listed properties:
   - `k3s_jobs_mcp_bearer_token` — `{"token": "<48+ random hex chars>"}`
   - `k3s_jobs_mcp_webhook_secret` — `{"secret": "<48+ random hex chars>"}`
   - `k3s_jobs_mcp_n8n_api_key` — `{"api_key": "<key created in the n8n UI,
     Settings → n8n API — a SECOND key labeled jobs-mcp, never the
     workbench's>"}`
   - `k3s_harbor_docker_pull_jobs` — `{"registry": "harbor.internal",
     "username": "<robot name>", "password": "<robot secret>"}` (robot
     account creation below).
2. **Harbor**: project `jobs` (private) + a robot account scoped to pull on
   that project only. The robot credential goes into the SM entry above.
3. **`cluster-vars` secret** (flux-system) — carries values that are used by
   manifests but never committed (ts.net FQDNs):
   ```bash
   kubectl create secret generic cluster-vars -n flux-system \
     --from-literal=N8N_TAILNET_FQDN=<n8n's full ts.net FQDN>
   ```
   Flux substitutes `${N8N_TAILNET_FQDN}` at reconcile time (postBuild on the
   `apps` Kustomization). If this secret is missing, the egress Service gets
   the literal placeholder and jobs-mcp cannot reach n8n — check here first
   when the bridge is down.
4. **n8n side**: every `jobs/*` webhook workflow starts with an IF node
   verifying the `X-Jobs-Webhook-Secret` header against the value in
   `k3s_jobs_mcp_webhook_secret`. Keep execution data retention
   (`saveDataOnSuccess`) at defaults so completed executions are inspectable.

## How it deploys

Flux chain `app-namespaces → app-secrets → apps`; the image is built and
pushed from the workbench:

```bash
cd services/jobs-mcp
docker buildx build --platform linux/amd64 -t harbor.internal/jobs/jobs-mcp:<semver> --push .
```

Deploys are image-tag bumps in `apps/base/jobs-mcp/deployment.yaml`.
Registry (task_type) changes hash-roll the Deployment automatically.
Rollback = revert the image-tag commit.

## Operational notes

- **Probes:** liveness never writes (full PVC ⇒ NotReady, not
  CrashLoopBackOff); readiness = DB write-ping + registry parsed + token
  loaded. n8n being down does NOT affect probes — enqueue keeps working,
  dispatch backs off, `jobs_bridge_up` goes 0.
- **Bridge down checklist:** `jobs_bridge_up == 0` → (1) is the n8n LXC up?
  (2) does the egress Service `n8n` in ns jobs-mcp have a real ExternalName
  (operator rewired it)? (3) does `cluster-vars` exist with the right FQDN?
  (4) tailscale ACLs still permit the operator's egress proxy → n8n:5678?
- **The egress Service** carries `kustomize.toolkit.fluxcd.io/ssa: Ignore` —
  Flux applies it once and then leaves it to the operator. To change its
  annotations, delete the Service and let Flux recreate it.
- **Alerts to add when alertmanager is wired:** `jobs_bridge_up == 0` for
  10 min; PVC > 80%; `jobs_queue_oldest_age_seconds` > 1h.
- **Cancel semantics:** queued only. A running n8n execution cannot be
  stopped (no public stop API); the registry `timeout_s` bounds the damage.
