# Kube-prometheus-stack
This app is provided by the prometheus community [here](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack). It adds Prometheus and Grafana for an out-of-the-box monitoring solution in the cluster.

Values are near-default with two deliberate exceptions (2026-09-01, see
`docs/runbooks/alerts.md`): the k3s-embedded control-plane component monitors
are disabled (permanent false positives on k3s), and Alertmanager delivers to
the n8n `alerts` webhook with a bearer credential mounted from the
ExternalSecret-synced `alerts-webhook-secret` (never in these values — repo is
public). Per-service alert rules live with their service, e.g.
`apps/base/jobs-mcp/prometheusrule.yaml`.