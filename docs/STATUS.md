# STATUS

Rolling status of the personal-cloud buildout. Updated at the end of every working
session. Tailnet MagicDNS names only — no LAN IPs or site details in this file.

_Last updated: 2026-09-01_

## Standing infrastructure

- **k3s cluster** — single node (`k3s`), managed by Flux from `main` of this repo.
  Running: harbor (private registry, homelab root CA via cert-manager), jupyterhub,
  kube-prometheus-stack, personal-site, tailscale-operator (kubectl over tailnet).
- **Tailnet** is the only network; nothing is publicly exposed except the
  Lightsail proxy path for the personal site.
- **Gateways:** compute site HA pair `tailscale-gw` + `tailscale-gw2` (server
  subnet + exit node, automatic failover); edge site `tailscale-gw-edge`
  (servers-VLAN subnet + exit node — single until a second edge host exists;
  the workbench's exit node stays advertised as a deliberate backup).
  Recipe: `proxmox/tailscale-gw.md`.
- **Bulk NAS:** `truenas-bulk-52tb` (TrueNAS Core VM) — first-class tailnet node.
  Replication targets / NFS exports / artifact uploads address it by this name,
  never by LAN IP. Artifacts dataset: `BulkPoolZ2/artifacts`. See
  `proxmox/nas-vm.md`.
- **Worker:** `worker-01` (LXC, compute site) — Node LTS + Claude Code,
  subscription-lane auth via on-host token file. Smoke test proven end-to-end
  (headless `claude -p` → JSON artifact + log land on the NAS by tailnet name).
  See `proxmox/worker.md`, `workers/smoke/`.
- **n8n:** `n8n` (LXC, compute site) — native npm under systemd, listening on its
  tailnet address only: `http://n8n:5678`. See `proxmox/n8n.md`,
  `mini/mcp-config.md`.
- **jobs-mcp — LIVE (2026-09-01):** the task-queue MCP service on k3s at
  `http://jobs-mcp/mcp` (bearer-gated, tailnet-only). SQLite queue on PVC,
  deterministic n8n executors via the operator egress Service, artifacts
  convention on the bulk NAS. First job proven end-to-end
  (`enqueue → running → succeeded`, attempts:1, through the ACL-gated
  egress). Spec `docs/specs/jobs-mcp.md`; runbook
  `docs/runbooks/jobs-mcp.md`; source `services/jobs-mcp/`.
- **Proxmox hosts on tailnet:** `dellpve` (compute), `naspve` (storage/NAS),
  `edgepve` (edge — host tailscale is its management path; see
  `proxmox/edgepve.md`).
- **Appliance tier (do not modify):** gateway LXCs, Pi-hole, NAS VM internals,
  storage pools, Tailscale ACLs.

## Session log — agent-stack kickoff (2026-08-27)

All five steps completed:

| Step | Result |
|------|--------|
| 1 | `tailscale-gw2` built on the storage host; both gateways verified advertising identical routes; fixed gw-01's non-persistent forwarding sysctls (would not have survived a reboot) |
| 2 | NAS VM joined tailnet as `truenas-bulk-52tb` (plain node, key expiry off); TrueNAS-specific install documented |
| 3 | `worker-01` built; smoke test passed end-to-end; artifact on `BulkPoolZ2/artifacts/worker-smoke/` |
| 4 | `n8n` built, tailnet-bound only, UI serving |
| 5 | this file, commits per step, verification checklist delivered |

## Manual follow-ups (operator)

- **n8n first run:** open `http://n8n:5678`, create the owner account, then
  Settings → n8n API → create an API key and export it as `N8N_API_KEY` on the
  workbench (`mini/mcp-config.md`).
- ~~Delete the smoke-test cron~~ done 2026-08-31 (7 hourly artifacts landed
  on the NAS while it ran — the lane works unattended).
- **After any TrueNAS upgrade:** re-run the tailscale package install on
  `truenas-bulk-52tb` (`proxmox/nas-vm.md`); the rc tunable survives.

## Workbench (2026-08-31)

`agent-mini` is live on the rack with JetKVM out-of-band console
(`jetkvm-hot-edge`). Bootstrapped no-sudo: Claude Code, node LTS, kubectl,
n8n MCP (connected), kubeconfig, and its own SSH key trusted by `dellpve`,
`naspve`, `truenas-bulk-52tb`. Remaining interactive steps in
`mini/setup.md`.

## Edge site buildout (2026-09-01)

The parked edge N150 is live: `edgepve` (dedicated untagged mgmt NIC +
VLAN-aware 10G guest trunk, PVE 9, restart-proven — `proxmox/edgepve.md`)
with first tenant `tailscale-gw-edge` (CT 101, servers-VLAN leg only).
Standing decision recorded in the host doc: the gateway never gets a
mgmt-VLAN leg — the host's own tailnet membership is the management path.
Beyond gateway duty the box remains expected-underused; candidate roles
unchanged (cross-site watchdog, second DNS/Pi-hole, small edge worker for
latency-sensitive jobs once jobs-mcp lands) — still no commitment.

## Pulse check (2026-09-01, session start)

All four inherited-stack checks green: jobs-mcp `healthz`/`readyz` 200 over
the tailnet; smoke-heartbeat `enqueue → succeeded` (attempts:1, ~700 ms
through the n8n bridge); all 8 Flux kustomizations Ready at `main`; all three
gateways (`tailscale-gw`, `tailscale-gw2`, `tailscale-gw-edge`) online and
advertising. ServiceMonitor confirmed scraping (`jobs_bridge_up`=1).

Findings to know about (none block work, all pre-existing):

- **Node disk at 73%** (98 GiB fs, 71 GiB used). Under `local-path`,
  `kubelet_volume_stats_*` for any PVC reports the *node filesystem*, not the
  claim — so the spec's "PVC > 80%" alert is really a node-disk alert and is
  only ~7 points from firing on day one. The 1 Gi claim itself is unenforced;
  `jobs_db_bytes` (28 KiB today) is the app-level complement.
- **Alertmanager routes to nowhere**: the kube-prometheus-stack HelmRelease
  carries no `values:` at all — chart-default Alertmanager config, everything
  to the null receiver. Alerts fire invisibly today (hence this session's
  alert-wiring step).
- **Standing false positives from chart defaults on k3s**:
  `KubeProxyDown`/`KubeSchedulerDown`/`KubeControllerManagerDown` fire
  permanently (k3s embeds those components; there is nothing to scrape).
  Must be disabled in values before any real receiver is wired.
- **`phyt-system` stuck job (not this repo's workload)**: namespace is
  Flux-labeled but no manifests live here and the cluster's only Flux source
  is this repo — orphaned-from-git tenant of the shared cluster.
  `minio-bucket-setup` has been `ImagePullBackOff` for 215 days
  (`minio/mc:RELEASE.2025-01-20T16-28-41Z`), keeping `KubeJobNotCompleted`
  permanently firing. Flagged to operator; not touched (destructive +
  not-ours rules).
- Cosmetic: kubelet exports a stale duplicate `kubelet_volume_stats_*` series
  for `jobs-mcp-data` labeled `namespace="default"` (no such PVC/pod exists);
  alert rules scope `namespace="jobs-mcp"` and are immune.

## Parked (deliberate, not forgotten)
- **~~knowledge-mcp vector store~~ — DECIDED 2026-09-02** (spec
  `docs/specs/knowledge-mcp.md` §1, panel + adversarial critique):
  candidate (c) sqlite-vec, loaded in-process into the service's own
  SQLite file at the vectors seam. pgvector-on-edgepve rejected (server on
  residential power + cross-site query triangle for ~10³ chunks) with
  numeric REOPEN TRIPWIRES the operator adopted: >250K chunks, OR a
  second non-MCP SQL consumer, OR p95 search >500ms. The N150 keeps no
  role from this service; its candidate roles list is unchanged.
- **Alertmanager :9093 NetworkPolicy** (2026-09-02): the port is
  unauthenticated ClusterIP and any pod can forge alerts (which then ride
  the real webhook bearer) or silence real ones. Accepted deliberately
  for a single-operator cluster (documented in docs/runbooks/alerts.md);
  the follow-up is a NetworkPolicy restricting ingress to Prometheus +
  operator pods.
- **Terminal notification channel** (2026-09-02): alerts now flow
  Alertmanager → n8n `alerts-webhook` workflow, but the last hop to a
  human needs a one-time operator step in the n8n UI — attach a
  notification node (email/Telegram/push) after "Respond 200" on the
  authorized branch, then re-export to n8n/. Until then, n8n execution
  history is the delivery record. Related seam: an external dead-man's
  snitch on the Watchdog alert (compute site is a single fate domain for
  the whole alerting stack).
- **Redis at compute site** — deploy only when its first real consumer
  lands (likely the model gateway: counters, cache, pub/sub); not before.
- **Lightsail proxy terraform drift** (found 2026-09-01): a full
  `terraform apply` wants to REPLACE the public proxy instance (user_data
  can't be reproduced — the original tailscale auth key was one-time — plus
  provider-schema drift on its public-ports resource) and tag-tweak one IAM
  user. Harmless today (secret changes go through targeted applies), but the
  proxy needs its own maintenance window: fresh tailscale auth key, brief
  public-site downtime, then untargeted applies are clean again.
- **Harbor UI on the tailnet** (2026-09-01): expose the portal via the
  tailscale operator (same `tailscale.com/expose` pattern as personal-site)
  so the UI needs no subnet-route/hosts-file setup on clients. Additive and
  cheap. The registry *hostname* stays `harbor.internal` — renaming it is a
  real migration (image refs, pull secrets, containerd trust, CA SANs,
  build scripts) with no current forcing event.

## jobs-mcp v1 shipped (2026-08-31 → 2026-09-01)

Spec (panel-designed, four sign-offs) → slice 1 (service, 70 tests, two
adversarial review rounds — 28 findings fixed across them) → slice 2
(manifests, terraform-managed secrets, operator egress with never-committed
FQDN via Flux postBuild substitution) → first live job succeeded end-to-end.
The one deliberate ACL change: `tag:k8s → n8n:5678` (documented here because
ACLs are otherwise appliance-tier). PRs #2, #3.

## Session log — alerts wired + knowledge-mcp born (2026-09-01 → 02)

Both PRs carry full adversarial reviews and satisfied merge gates; merging
is the operator's call:

- **PR #6 — alert wiring.** Receiver decision (operator): Alertmanager →
  n8n webhook over the existing jobs-mcp egress + ACL grant, fail-closed
  401 on bad bearer, phyt-system null-routed (operator pick), k3s
  component false-positives disabled, chart pinned 88.6.2, prod-only
  scoping (dev keeps only the disables). Live prereqs DONE: SM secret
  (targeted apply, 2-add/0-destroy plan), n8n env + restart, workflow
  imported/active, delivery tested 200/401. Review: 3 agents, 1 HIGH +
  4 MED + 6 LOW, all taken or spun off. Runbook:
  `docs/runbooks/alerts.md`.
- **knowledge-mcp spec APPROVED** (2026-09-02, four sign-offs: pinned pod
  fetch / caller-token JSON map, delegated / homelab-notes operator-only
  with curated NanoClaw corpus later / CI in slice 1). Process: 4-designer
  panel with opposed lenses → 2 adversarial critics (bench-verified
  claims) → synthesis. Spec: `docs/specs/knowledge-mcp.md`.
- **PR #7 — knowledge-mcp slice 1** (code + tests + eval + CI only; no
  manifests, nothing reconciles). 74 unit tests; golden eval 25/25
  recall@5, MRR 0.94, blind spot kept as expect_miss; the repo's FIRST
  working CI (typecheck+tests+eval, doc-path triggered) ran green on its
  own PR in 16s. Review: 3 agents, 18 findings taken — headline fixes:
  chunker-version stamp + boot re-chunk (silent chunk-identity drift),
  reingest tombstone circuit breakers, clean-sweep-only freshness,
  §7 untrusted envelopes implemented, percent-encoding rejected outright.
- Spun-off task chips: external-secrets IAM identity under terraform;
  jobs-mcp bridge-gauge idle-blindness (timer probe).

## Next session starts with

- **knowledge-mcp slice 2** (after PR #7 merges): manifests + secrets +
  registry ConfigMap, MERGE GATE per spec §8 (SM entry
  `k3s_knowledge_mcp_caller_tokens` via targeted terraform apply, Harbor
  `knowledge` project + pull robot + image pushed from the workbench,
  base joins apps/homelab-prod ONLY — never apps/development).
- **knowledge-mcp slice 3**: `knowledge-reingest` jobs-mcp task_type +
  n8n workflow + scoped `n8n-reingest` token + end-to-end proof.
- **Operator one-timers**: merge PRs #6/#7; attach the terminal
  notification channel in the n8n UI (Parked entry); the two task chips.
- Still open from build order item 1: storage classes + site labels.

## Decisions log

- 2026-08-27: repo is public, so the agent-harness file (CLAUDE.md) and session
  kickoff prompts stay local (gitignored) rather than committed — they contain
  context that the repo's own rules keep out of published docs. Flip by removing
  the .gitignore entries if the repo ever goes private.
- 2026-08-27: worker artifacts get a dedicated dataset (`BulkPoolZ2/artifacts`)
  rather than a directory in the existing share — own snapshot/quota policy later.
- 2026-08-27: n8n runs native npm, not docker-in-LXC — one less runtime layer in
  an unprivileged CT.
- 2026-08-27: smoke artifacts travel by scp, not NFS mount — needs nothing but
  SSH already enabled on the NAS; revisit at real artifact volume.
- 2026-08-30: tailnet ts.net FQDNs are never committed to this public repo —
  where a manifest needs one (jobs-mcp n8n egress), it ships as a gitignored
  overlay patch.
- 2026-09-01: alert receiver is the n8n webhook (operator pick over
  ntfy/email) — zero new infra, rides the proven egress + ACL grant, seeds
  the notifications lane; phyt-system alerts null-routed (muted, not fixed).
- 2026-09-02: knowledge-mcp auth ships the caller-token JSON MAP shape (one
  SM secret, id→token; policy in the repo registry) and it is the HOUSE
  TARGET SHAPE — jobs-mcp adopts it at its NanoClaw retrofit. Decided over
  scalar parity because the scheduled-reingest executor is a second caller
  on day one and a later scalar→map reshape breaks every holder atomically.
- 2026-09-02: homelab-notes stays operator-only permanently; NanoClaw will
  be served a deliberately curated corpus (e.g. homelab-faq) instead of the
  working notes (conversational-exfiltration fence).
- 2026-09-02: shared-base HelmRelease values that are prod-only (receiver
  config, secret mounts) live as apps/homelab-prod PATCHES, not in the base
  — apps/development consumes the same bases (learned from the alerts
  review; the jobs-mcp in-base-secrets precedent only works because that
  base is excluded from dev).
