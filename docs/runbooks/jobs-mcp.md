# Runbook: jobs-mcp

Operator-facing steps only — semantics (states, probes, cancel, alerts) live
in the spec: `docs/specs/jobs-mcp.md`. Manifests: `apps/base/jobs-mcp/`.
Source: `services/jobs-mcp/`.

## MERGE GATE — order is load-bearing

The apps chain uses `wait: true` with `dependsOn`, so an unready jobs-mcp
freezes reconciliation of infra-network and slows the whole chain. Do NOT
merge the deploy PR until ALL of these exist:

1. The four AWS SM entries (below).
2. The Harbor `jobs` project + robot, and the image
   `harbor.internal/jobs/jobs-mcp:<tag>` **pushed**.
3. The `cluster-vars` secret on-cluster (below).

## One-time prerequisites

1. **AWS Secrets Manager entries — terraform-managed** (`terraform/main.tf`,
   same pattern as every other `k3s_*` secret): set the five
   `jobs_mcp_*`/`jobs_harbor_*` values in your `terraform.tfvars` (template:
   `terraform.tfvars.empty`) and `terraform apply`. Value sources:
   - `jobs_mcp_bearer_token` / `jobs_mcp_webhook_secret` — generated at
     slice-2 time. The webhook secret's SAME value must be present as
     `JOBS_WEBHOOK_SECRET` in the n8n LXC's `/etc/n8n/n8n.env` (workflows
     verify the header against `$env`).
   - `jobs_mcp_n8n_api_key` — second key from the n8n UI (Settings → n8n
     API), labeled `jobs-mcp`. Optional at runtime (non-fatal startup check)
     but the ExternalSecret still wants it to sync.
   - `jobs_harbor_docker_pull_username` / `_password` — the pull-only robot
     from step 2 (full `robot$jobs+...` name).
2. **Harbor**: private project `jobs`; robot scoped to pull on it; your
   docker-push user as maintainer.
3. **`cluster-vars` secret** (flux-system) — values used by manifests but
   never committed (ts.net FQDNs):
   ```bash
   kubectl create secret generic cluster-vars -n flux-system \
     --from-literal=N8N_TAILNET_FQDN=<n8n's full ts.net FQDN>
   ```

## Deploy / rollback

Image is built and pushed from the workbench; deploys are image-tag bumps in
`apps/base/jobs-mcp/deployment.yaml`; registry (task_type) changes hash-roll
the Deployment. Rollback = revert the tag commit.

```bash
cd services/jobs-mcp
docker buildx build --platform linux/amd64 -t harbor.internal/jobs/jobs-mcp:<semver> --push .
```

## Bridge down (`jobs_bridge_up == 0`) checklist

1. Is the n8n LXC up? (`http://n8n:5678` from any tailnet device)
2. Does Service `n8n` in ns `jobs-mcp` exist with a real ExternalName (the
   operator rewrites it)? If it holds the literal `${N8N_TAILNET_FQDN}`,
   `cluster-vars` was missing at creation — **delete the Service** and let
   Flux recreate it (it is `ssa: IfNotPresent`; Flux will not heal it in
   place).
3. Does `cluster-vars` exist in flux-system with the right FQDN?
4. Do the tailscale ACLs still permit the operator's egress proxy →
   n8n:5678?

## Disaster-recovery rebuild ordering

On a from-scratch rebuild, create `cluster-vars` BEFORE Flux bootstrap —
otherwise the egress Service is created with a literal placeholder FQDN and
must be deleted to heal (see checklist item 2). The first reconcile may also
race the kube-prometheus-stack CRDs (ServiceMonitor); `retryInterval: 2m` on
the apps Kustomization bounds that to a couple of minutes.

## n8n side

Every `jobs/*` webhook workflow verifies `X-Jobs-Webhook-Secret` against
`$env.JOBS_WEBHOOK_SECRET` in its first node (spec §6, v1-mandatory; the env
var lives in `/etc/n8n/n8n.env`, never in workflow JSON). Keep execution-data
retention at defaults so completed executions stay inspectable.
