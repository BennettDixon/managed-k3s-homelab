# n8n workflows

Canonical exports of workflows running on the tailnet-bound n8n instance
(`http://n8n:5678`, see `proxmox/n8n.md`). Edit strategy: build/adjust via the
API or UI, then export back here (`GET /api/v1/workflows/<id>` filtered to
`{name, nodes, connections, settings}`) so the repo stays the source of truth.

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `smoke-heartbeat.json` | `POST /webhook/jobs/smoke-heartbeat` | jobs-mcp executor contract (spec §6): verifies `X-Jobs-Webhook-Secret` against `$env.JOBS_WEBHOOK_SECRET` in its first node, returns the completion report `{ok, result, artifacts, spent_usd}`. Migrated from the original GET heartbeat 2026-09-01. |
| `alerts-webhook.json` | `POST /webhook/alerts` | Alertmanager receiver (docs/runbooks/alerts.md): verifies `Authorization: Bearer` against `$env.ALERTS_WEBHOOK_SECRET`, formats the alert group into a `headline`, responds 200 on the authorized branch and **401 otherwise** (Alertmanager must see auth failures — 2xx would silently drop alerts during rotation skew). Terminal channel: a Telegram node after **Respond 200** on the authorized branch only — chat id via `$env.ALERTS_TELEGRAM_CHAT_ID` (LXC env, never in this JSON), bot token in the instance credential store. Sitting after the respond node means Telegram latency/failures never affect Alertmanager's 200; a failed send shows as an errored execution. |
| `knowledge-reingest.json` | `POST /webhook/jobs/knowledge-reingest` | jobs-mcp executor for the `knowledge-reingest` task_type (knowledge spec §6): verifies `X-Jobs-Webhook-Secret` in its first node, then its ONLY action is one MCP `reingest` call to `http://knowledge-mcp/mcp` with the scoped `n8n-reingest` token from `$env.KNOWLEDGE_REINGEST_TOKEN`; returns the completion report. A sweep with per-doc errors is reported `ok:false` (index_as_of did not advance) so jobs-mcp retries and the failure is visible in job state. |
| `knowledge-reingest-nightly.json` | Schedule, `30 3 * * *` (instance timezone) | Enqueues `knowledge-reingest {corpus: homelab-notes}` on jobs-mcp (`budget_cap: 0`, day-keyed `idempotency_key` so a re-fire is a replay) using `$env.JOBS_MCP_BEARER_TOKEN`. Imported INACTIVE: arming it means the jobs-mcp bearer gains a second holder (the n8n env) — an explicit operator decision, see `docs/runbooks/knowledge-mcp.md` "Scheduled freshness". |

Secrets never live in exports: n8n credentials stay in the instance's own
credential store; the API key used to manage workflows stays in the
workbench environment (`N8N_API_KEY`, see `mini/mcp-config.md`).
