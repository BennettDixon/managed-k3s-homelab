# Runbook: alert delivery (Alertmanager → n8n)

Wired 2026-09-01. Rules live with their service (first set:
`apps/base/jobs-mcp/prometheusrule.yaml`); delivery config lives in
`apps/base/kube-prometheus-stack/release.yaml` values. Receiver decision
(operator, 2026-09-01): n8n webhook — zero new infra, canonical workflow in
`n8n/alerts-webhook.json`, seeds the future notifications lane.

## Path

```
PrometheusRule (release: kube-prometheus-stack label, required)
  → Prometheus → Alertmanager (k3s cluster)
  → webhook http://n8n.jobs-mcp.svc.cluster.local:5678/webhook/alerts
    (jobs-mcp's operator egress Service — accepted coupling: alert delivery
    depends on the jobs-mcp namespace; if jobs-mcp ever moves, give
    kube-prometheus-stack its own ExternalName egress Service)
  → n8n workflow `alerts-webhook` (verifies Authorization: Bearer against
    $env.ALERTS_WEBHOOK_SECRET; formats a headline)
  → terminal channel: attach a notification node after "Verify And Format"
    in the n8n UI (email/Telegram/push — needs a credential only the
    operator can add), then export back to n8n/ per its README.
    Until then, n8n execution history is the delivery record.
```

Muted routes (both re-declared in values because Helm replaces the default
list): `Watchdog` (chart default), `namespace=phyt-system` (operator decision
2026-09-01 — another project's orphaned workload keeps `KubeJobNotCompleted`
firing; still visible in Prometheus, delete the route when that project gets
attention). Also disabled: kubeProxy/kubeScheduler/kubeControllerManager
component monitors — k3s embeds them, the chart-default monitors were
permanent false positives.

## MERGE GATE — order is load-bearing

An unready ExternalSecret freezes the apps Kustomization (`wait: true`)
cluster-wide. Do NOT merge the wiring PR until ALL of:

1. `k3s_alerts_webhook_secret` exists in AWS SM — `alerts_webhook_secret` in
   `terraform.tfvars`, then a **TARGETED** apply
   (`terraform apply -target=module.alerts_webhook_secret` — full applies
   stay forbidden while the Lightsail proxy drift is parked, see STATUS):
2. The same value is live in the n8n LXC env as `ALERTS_WEBHOOK_SECRET`
   (`/etc/n8n/n8n.env`) and n8n restarted.
3. Workflow `alerts-webhook` imported and ACTIVE on n8n (`n8n/alerts-webhook.json`).

Post-merge, Alertmanager reloads with the new config; a 404/401 from the
webhook only logs delivery errors in Alertmanager — annoying, not fatal.

## Verify after merge

```bash
# 1. ExternalSecret synced
kubectl -n kube-prometheus-stack get externalsecret alerts-webhook-secret
# 2. Alertmanager took the config (restarts on secret/config change)
kubectl -n kube-prometheus-stack get pods | grep alertmanager
# 3. Rules loaded
kubectl -n jobs-mcp get prometheusrule
# 4. End-to-end: port-forward Alertmanager and fire a synthetic alert
kubectl -n kube-prometheus-stack port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 &
curl -sS -X POST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d \
  '[{"labels":{"alertname":"SyntheticTest","namespace":"jobs-mcp","severity":"warning"},"annotations":{"summary":"synthetic delivery test"}}]'
# then check n8n executions for an alerts-webhook run (~group_wait 30s later)
```

## Rotation

Rotate `alerts_webhook_secret` in tfvars → targeted apply → update
`ALERTS_WEBHOOK_SECRET` in `/etc/n8n/n8n.env` + restart n8n → wait for the
ExternalSecret refresh (≤1h, or annotate to force) → Alertmanager pod picks
up the mounted file on its own (credentials_file is read per-request).

## Named seams (not built)

- **Dead-man's snitch:** Alertmanager, Prometheus, and n8n all share
  compute-site fate — a site-down event delivers nothing. The `Watchdog`
  alert exists precisely to be pointed at an EXTERNAL heartbeat monitor
  (healthchecks.io, or an edgepve-hosted watcher — a candidate edge role).
- **Severity routing:** everything is `severity: warning` today, one route;
  page-vs-digest splits belong to the notifications lane when workers land.
