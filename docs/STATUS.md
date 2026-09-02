# STATUS

Rolling status of the personal-cloud buildout. Updated at the end of every working
session. Tailnet MagicDNS names only — no LAN IPs or site details in this file.

_Last updated: 2026-09-02_

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
- **knowledge-mcp — LIVE (2026-09-02):** the retrieval MCP service on k3s at
  `http://knowledge-mcp/mcp` (caller-token JSON map, tailnet-only).
  SQLite+FTS5 index on PVC — a cache rebuilt from GitHub by `reingest`;
  corpus #1 `homelab-notes` (docs/ + proxmox/). Freshness rides jobs-mcp's
  `knowledge-reingest` task (n8n executor with the scoped `n8n-reingest`
  token); the nightly trigger is imported but INACTIVE pending the
  operator's credential decision. Spec `docs/specs/knowledge-mcp.md`;
  runbook `docs/runbooks/knowledge-mcp.md`; source
  `services/knowledge-mcp/`; manifests `apps/base/knowledge-mcp/`.
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

## Pulse check (2026-09-02, session start)

All inherited-stack checks green: jobs-mcp `healthz`/`readyz` 200 over the
tailnet; smoke-heartbeat `enqueue → succeeded` (attempts:1, ~300 ms through
the n8n bridge); knowledge-mcp and manifests CI both green on `main`; a
synthetic alert `Alertmanager → n8n` ran as a successful `alerts-webhook`
execution (Telegram leg is the operator's confirmation); all 8 Flux
kustomizations Ready at `main`; three gateways online and advertising —
`tailscale-gw` primary for the compute subnet, `tailscale-gw2` standby with
the identical route approved, `tailscale-gw-edge` primary for the edge
subnet; `jobs_bridge_up`=1 scraped.

Findings (none block work; all pre-existing):

- **Node disk 72.7% by the alert's own ratio** (kubelet used/capacity:
  71.1 of 97.9 GiB — flat since 09-01's 73%; node-exporter's avail-based
  view says 77.8% because it counts ext4 reserved blocks). Not imminent,
  but the headroom is the image filesystem: 29.2 GiB of container images,
  Prometheus TSDB 3.5 GiB, the rest local-path PVC data (Harbor registry,
  phyt-system timescaledb). Kubelet's own image GC only starts at 85%,
  above the 80% alert. Remedy when it matters is an operator call
  (destructive): prune unused images on the node (`k3s crictl rmi
  --prune`) and review the Harbor registry share.
- **Harbor admin password drift**: the AWS SM value
  (`k3s_harbor_admin_password`, synced into the `harbor-admin-password`
  secret) no longer matches the live admin login (401) — the chart reads
  `existingSecretAdminPassword` at install only and the password was
  changed in the UI since. Admin API access is therefore not
  reconstructable from terraform. Workaround used this session: the
  `knowledge` project was created through the API as the operator's
  `docker-push` user (scripted from the Docker credential store — the
  password never touched a command line; creator = project admin, demoted
  to maintainer afterwards, `bennett` added as project admin). Fix is the
  operator's: reset the admin password to the SM value, or put the live
  value into SM (targeted apply).
- **AWS SSO session expired** at session start; after the operator's login
  the targeted apply ran clean (4 add / 0 change / 0 destroy) and #9 merged.
- **Golden eval blind spot closing**: an `expect_miss` query ("how much
  does it cost to run a job") now hits — promote it in
  `services/knowledge-mcp/eval/golden.yaml` (improvement, not failure).

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
- **~~Terminal notification channel~~ — DONE 2026-09-02**: Telegram node
  wired after "Respond 200" on the authorized branch (operator created the
  bot + credential in the n8n UI; chat id rides `$env` in the LXC, never
  in git; canonical export updated). Alerts reach the operator's phone
  end-to-end. Still parked from that thread: an external dead-man's
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

## Session log — knowledge-mcp deployable + scheduled freshness (2026-09-02, evening)

Both PRs carry three-lens adversarial reviews and their MERGE GATEs; merging
and the one remaining gate step are the operator's:

- **PR #9 — knowledge-mcp slice 2** (manifests, secrets, Harbor + CI wiring)
  — **MERGED and LIVE 2026-09-02**: targeted terraform apply of
  `k3s_knowledge_mcp_caller_tokens` + `k3s_harbor_docker_pull_knowledge`
  (4 add / 0 destroy), Flux at the merge commit ~80 s later, pod Ready with
  both ExternalSecrets synced and the image pulled through the new robot,
  first operator `reingest` indexed 11 docs / 93 chunks at `main@f5043ad`
  in 2 s, search proven over the tailnet, Prometheus scraping with all four
  rules loaded. Pre-merge: Harbor `knowledge` project + pull robot, image
  `0.1.0` proven read-only/non-root, registry validated in CI. Review: 3 agents, 24 findings,
  all but three taken — headline: `KnowledgeIndexNeverBuilt` (the stale alert
  was blind to a never-built corpus), pod hardening (no SA token, read-only
  rootfs, no caps), runbook diagnostics that work from kubectl.
- **PR #11 — knowledge-reingest task_type** (first opened as #10 stacked
  on the slice-2 branch; that merge landed on the branch, not `main` — same
  reviewed content re-targeted): jobs-mcp
  registry entry + executor workflow (imported, ACTIVE, proven through a
  temporary copy against a local instance: authorized → completion report;
  forged / out-of-scope / malformed → fail closed) + nightly trigger
  (imported INACTIVE). `KNOWLEDGE_REINGEST_TOKEN` is live in the n8n env.
  **MERGED 2026-09-02 and PROVEN end to end:** jobs-mcp hash-rolled on the
  registry change and came up listing both task types; `enqueue → running
  → succeeded` (attempts:1, spent 0) with `result.source_ref` =
  `main@78c1fa1`; knowledge-mcp `index_as_of.commit` advanced to the same
  sha (3 docs re-indexed, 8 unchanged, 96 chunks); the n8n execution
  succeeded. Review: 2 agents (executor contract; security/identity), 11 findings, all taken — the dispatcher timeout was shorter than the executor's (orphaned executions on a slow night), error messages now carry what jobs-mcp actually preserves, jobs-mcp gained its first CI workflow with a deployed-registry guard (spec §4's CI-checkable invariant).
- **Operator decision parked in the runbook:** arming the nightly trigger
  puts the jobs-mcp bearer into the n8n env (second holder of the operator
  credential). Alternative: an in-cluster CronJob via ExternalSecret.
- Harbor admin password drift and the node-disk numbers: see the pulse
  check above.

## Next session starts with

- **knowledge-mcp v1 is LIVE end to end** (#9 + #11, 2026-09-02): serving at
  `http://knowledge-mcp/mcp`, registered on the workbench, scheduled
  freshness proven through jobs-mcp, and the nightly direct schedule
  (`knowledge-reingest-direct`, decision above) armed on n8n. Operator
  decision still open: keep or strike the fourth alert
  (`KnowledgeIndexNeverBuilt`). Per-caller jobs-mcp tokens (the NanoClaw
  retrofit) are what re-arm the queue-shaped nightly.
- **Next build-order item: the model gateway spec** (item 4). Language is
  an explicit sign-off question (Python vs TS-for-parity, two-toolchain cost
  stated); lanes: subscription via headless Claude on workers, metered API
  fallback; per-project ledger; request logging. Design-first with the
  panel + critics treatment jobs-mcp and knowledge-mcp got.
- **Awaiting the operator's pick** (one sharp question, not a guess): the
  two parked chips (external-secrets IAM identity under terraform;
  jobs-mcp bridge-gauge idle-blindness probe) or build-order item 1
  (storage classes + site labels).
- Housekeeping from the pulse check: Harbor admin password drift, node
  disk headroom; still parked: the dead-man's snitch on the Watchdog alert.

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
- 2026-09-02: nightly knowledge freshness runs as a DIRECT `reingest`
  schedule (n8n Schedule trigger → knowledge-mcp, with the scoped
  reingest-bot token n8n already holds) — zero new secrets, no job row;
  spec §6 deviation by sign-off (SIGN-OFF 5). The queue-shaped scheduler
  stays exported and INACTIVE until jobs-mcp has per-caller tokens, when a
  caller allowed only `enqueue knowledge-reingest` takes over. The v1 jobs
  bearer never enters the n8n env: it would hand every workflow author the
  whole jobs-mcp surface.
- 2026-09-02: shared-base HelmRelease values that are prod-only (receiver
  config, secret mounts) live as apps/homelab-prod PATCHES, not in the base
  — apps/development consumes the same bases (learned from the alerts
  review; the jobs-mcp in-base-secrets precedent only works because that
  base is excluded from dev).
