# managed-k3s-homelab

Infrastructure monorepo for a two-site personal cloud: a Flux-managed k3s
cluster plus everything around it, joined by one Tailscale tailnet (the only
network — nothing is publicly exposed except a small proxy for the personal
site).

| Path | What lives there |
|------|------------------|
| `apps/`, `clusters/`, `infrastructure/` | Flux GitOps tree (kustomize bases + per-cluster overlays) |
| `services/` | Service source code (currently `jobs-mcp`, the task-queue MCP service) |
| `n8n/` | Canonical n8n workflow exports (secret-free) |
| `proxmox/` | Parameterized host/LXC/VM recipes and host notes |
| `mini/` | Workbench (Mac mini) setup and MCP configuration |
| `terraform/` | AWS-side resources: Secrets Manager entries, IAM, the public proxy |
| `workers/` | Worker-node scripts (subscription-lane smoke test) |
| `docs/` | Rolling `STATUS.md`, approved specs, operator runbooks |

Start with [docs/STATUS.md](docs/STATUS.md) for current state and
[docs/specs/jobs-mcp.md](docs/specs/jobs-mcp.md) for the first shipped
service. Conventions: tailnet MagicDNS names only in anything durable, no
secrets in git (external-secrets ← AWS Secrets Manager ← terraform), every
service ships probes/limits/README, and manual steps live in runbooks.
