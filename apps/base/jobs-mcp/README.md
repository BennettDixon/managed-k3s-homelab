# jobs-mcp (manifests)

Task-queue MCP service of the personal cloud: `enqueue`/`status`/`artifacts`/
`cancel` over MCP at `http://jobs-mcp/mcp` (tailnet-only via the tailscale
operator), jobs persisted in SQLite on `jobs-mcp-data` (local-path PVC),
dispatched to n8n workflows through the operator egress Service `n8n` in this
namespace. Depends on: external-secrets (three ExternalSecrets in
apps/secrets/), the Harbor `jobs` project robot for image pulls, and the
`cluster-vars` secret in flux-system supplying `${N8N_TAILNET_FQDN}` at
reconcile time (never committed). Source: `services/jobs-mcp/`. Spec:
`docs/specs/jobs-mcp.md` (approved). Operator runbook:
`docs/runbooks/jobs-mcp.md`.
