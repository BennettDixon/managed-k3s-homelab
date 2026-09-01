# n8n workflows

Canonical exports of workflows running on the tailnet-bound n8n instance
(`http://n8n:5678`, see `proxmox/n8n.md`). Edit strategy: build/adjust via the
API or UI, then export back here (`GET /api/v1/workflows/<id>` filtered to
`{name, nodes, connections, settings}`) so the repo stays the source of truth.

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `smoke-heartbeat.json` | `POST /webhook/jobs/smoke-heartbeat` | jobs-mcp executor contract (spec §6): verifies `X-Jobs-Webhook-Secret` against `$env.JOBS_WEBHOOK_SECRET` in its first node, returns the completion report `{ok, result, artifacts, spent_usd}`. Migrated from the original GET heartbeat 2026-09-01. |

Secrets never live in exports: n8n credentials stay in the instance's own
credential store; the API key used to manage workflows stays in the
workbench environment (`N8N_API_KEY`, see `mini/mcp-config.md`).
