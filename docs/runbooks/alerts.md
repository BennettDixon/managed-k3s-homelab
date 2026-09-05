# Runbook: alert delivery (Alertmanager → n8n)

Wired 2026-09-01. Rules live with their service (first set:
`apps/base/jobs-mcp/prometheusrule.yaml`); delivery config is PROD-ONLY in
`apps/homelab-prod/kube-prometheus-stack-values.yaml` + the ExternalSecret
beside it (the kube-prometheus-stack base also ships to apps/development —
only the k3s component-monitor disables belong in the base). Receiver
decision (operator, 2026-09-01): n8n webhook — zero new infra, canonical
workflow in `n8n/alerts-webhook.json`, seeds the future notifications lane.

## Path

```
PrometheusRule (release: kube-prometheus-stack label, required)
  → Prometheus → Alertmanager (k3s cluster)
  → webhook http://n8n.jobs-mcp.svc.cluster.local:5678/webhook/alerts
    (jobs-mcp's operator egress Service — accepted coupling: alert delivery
    depends on the jobs-mcp namespace; if jobs-mcp ever moves, give
    kube-prometheus-stack its own ExternalName egress Service)
  → n8n workflow `alerts-webhook` (verifies Authorization: Bearer against
    $env.ALERTS_WEBHOOK_SECRET; formats a headline; 200 on the authorized
    branch, 401 otherwise — fail-closed-and-loud, see "Failure visibility")
  → terminal channel: attach a notification node after "Respond 200" on the
    AUTHORIZED branch in the n8n UI (email/Telegram/push — needs a
    credential only the operator can add), then export back to n8n/ per its
    README. Never attach it before the Authorized? gate: the unauthorized
    branch must stay a dead end. Until then, n8n execution history is the
    delivery record.
```

## Failure visibility + accepted risks (decided, not accidental)

- **Bad/rotating bearer → 401, visible.** Alertmanager treats non-2xx as a
  failed notification: it logs and increments
  `alertmanager_notifications_failed_total` (4xx is not retried; 429/5xx
  are). A 200-on-unauthorized here would be a silent black hole — every
  rotation has a skew window (ExternalSecret refresh ≤1h behind the n8n env
  update), and during it alerts would be "delivered" to nowhere.
- **Alertmanager :9093 is unauthenticated ClusterIP** and the cluster has no
  NetworkPolicies: any in-cluster workload (including the orphaned
  phyt-system tenant) can forge alerts — which Alertmanager then forwards
  wearing the real bearer — or silence real ones. Accepted for now
  (single-operator cluster; in-cluster compromise is already severe), which
  is why the workflow caps and sanitizes alert-derived strings. Named
  follow-up: a NetworkPolicy restricting 9093 ingress to Prometheus +
  operator pods.
- **The bearer appears in n8n execution history** (webhook items persist
  request headers; executions are deliberately kept as the delivery
  record). Readable by any n8n UI/API identity, including the cluster-synced
  jobs-mcp API key. Same trust domain as the jobs webhook secret — accepted.
- **At real node-disk pressure expect a small alert pile-up** for the same
  disk: `JobsPvcDiskFilling` (80%) plus the chart's
  `KubePersistentVolumeFillingUp`, possibly duplicated by a stale
  `namespace="default"` kubelet series (see STATUS pulse-check note). Noisy
  but honest; don't be surprised.

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

## Monitoring state storage (added 2026-09-03)

Prometheus and Alertmanager ran on the chart-default `emptyDir` until now: on
the node filesystem, so every pod recreation discarded the TSDB, every silence
and the notification log — and nothing capped TSDB growth on a node already
~73% full. Both now take `local-path` claims, declared in the PROD values
patch only (`apps/homelab-prod/kube-prometheus-stack-values.yaml`); the shared
base stays clean because `apps/development` consumes it and a class that does
not exist there would leave the claim Pending and stall `apps` (wait: true).

This adds no disk usage — `emptyDir` was already on that filesystem. It makes
the usage named, restart-surviving and bounded.

**The cap that actually binds is `retentionSize: 8GiB`, not the 10Gi claim.**
local-path is a hostPath provisioner and does not enforce a requested size
(same as the unenforced jobs-mcp 1Gi claim). `retentionSize` governs persisted
blocks only — WAL and head are on top — which is why it sits well under the
claim. Blocks measured 3.19 GiB at 10d retention on 2026-09-03.

**One-time cost when this merges.** `volumeClaimTemplates` are immutable, so
prometheus-operator deletes and recreates each StatefulSet to apply the new
storage. Expect on the first reconcile:

- Prometheus starts with an empty TSDB — the current 10 days of history is
  gone. Dashboards look blank until data accumulates. Alert rules are
  unaffected (they live in PrometheusRules), but any rule with a long `for:`
  window needs that window to elapse again before it can fire.
- Alertmanager loses active silences and its notification log, so anything
  silenced must be re-silenced and already-sent alerts may notify once more.
  Check for live silences BEFORE merging; there were none on 2026-09-03.

**Decided (operator, 2026-09-03):** the existing history is dropped, not
snapshotted or exported first. Nothing in it is load-bearing yet — no ledger
reads it, no SLO baseline depends on it — and a TSDB snapshot would need a
hand-restore into the new claim for no consumer.

Verify after merge:

```bash
kubectl -n kube-prometheus-stack get pvc          # both claims Bound
kubectl -n kube-prometheus-stack get pods         # both StatefulSet pods Ready
# retention cap took effect (expect 8GiB / 8589934592):
kubectl -n kube-prometheus-stack get prometheus -o jsonpath='{.items[0].spec.retentionSize}{"\n"}'
# then re-run the end-to-end synthetic alert above — the receiver path is
# unchanged by this, but it is the cheapest proof the recreate went clean.
```

Rollback is removing the two blocks: the operator recreates the StatefulSets
on `emptyDir` again and the claims are left behind for manual cleanup
(deleting a PVC is destructive — an explicit operator task, never folded in).

## Named seams (not built)

- **Dead-man's snitch:** Alertmanager, Prometheus, and n8n all share
  compute-site fate — a site-down event delivers nothing. The `Watchdog`
  alert exists precisely to be pointed at an EXTERNAL heartbeat monitor
  (healthchecks.io, or an edgepve-hosted watcher — a candidate edge role).
- **Severity routing:** everything is `severity: warning` today, one route;
  page-vs-digest splits belong to the notifications lane when workers land.
