# STATUS

Rolling status of the personal-cloud buildout. Updated at the end of every working
session. Tailnet MagicDNS names only — no LAN IPs or site details in this file.

_Last updated: 2026-09-10_

## Standing infrastructure

- **k3s cluster** — single node (`k3s`), managed by Flux from `main` of this repo.
  Running: harbor (private registry, homelab root CA via cert-manager), jupyterhub,
  kube-prometheus-stack, personal-site, tailscale-operator (kubectl over tailnet).
  Prometheus and Alertmanager keep state on `local-path` claims since
  2026-09-05 (10 Gi with `retentionSize` 8 GiB; 1 Gi) — `docs/runbooks/alerts.md`.
  External-secrets authenticates as the terraform-managed, read-only
  `k3s-external-secrets-reader` since 2026-09-09
  (`docs/runbooks/external-secrets-iam.md`).
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
  egress). The idle bridge probe (#16) ships as 0.2.0 via PR #19 together
  with pod hardening parity and a version-bump CI guard (image pushed
  2026-09-09). Spec `docs/specs/jobs-mcp.md`; runbook
  `docs/runbooks/jobs-mcp.md`; source `services/jobs-mcp/`.
- **knowledge-mcp — LIVE (2026-09-02):** the retrieval MCP service on k3s at
  `http://knowledge-mcp/mcp` (caller-token JSON map, tailnet-only).
  SQLite+FTS5 index on PVC — a cache rebuilt from GitHub by `reingest`;
  corpus #1 `homelab-notes` (docs/ + proxmox/). Nightly freshness is the
  DIRECT `knowledge-reingest-direct` schedule on n8n (SIGN-OFF 5; firing
  daily at 07:30Z since 2026-09-03 with the index at `main`); the
  queue-shaped `knowledge-reingest-nightly` stays INACTIVE until jobs-mcp has
  per-caller tokens. Spec `docs/specs/knowledge-mcp.md`;
  runbook `docs/runbooks/knowledge-mcp.md`; source
  `services/knowledge-mcp/`; manifests `apps/base/knowledge-mcp/`.
- **gateway — slice 1 MERGED 2026-09-10 (PR #22), NOT DEPLOYED:** the model
  gateway's code, tests and CI (`services/gateway/`, the first Python service
  of the house; `.github/workflows/gateway.yml` with the version-bump guard).
  Nothing runs on the cluster until slice 2 lands the manifests behind the
  spec §8 MERGE GATE. **Slice 2 = PR #24 (opened 2026-09-10), draft behind
  its gate — reviewed, CI green, nothing on the cluster; image tag 0.1.1.** Spec `docs/specs/gateway.md` (as-built deltas at the
  top); source `services/gateway/` (README carries the verify line;
  `uv run gateway-smoke-local` proves the ledger against a stub upstream).
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
  value into SM (targeted apply). **RESOLVED 2026-09-03:** admin reset to
  the SM value through the `harbor_user` row (recipe in
  `apps/base/harbor/README.md`); terraform, Secrets Manager, the cluster
  secret and Harbor agree again.
- **AWS SSO session expired** at session start; after the operator's login
  the targeted apply ran clean (4 add / 0 change / 0 destroy) and #9 merged.
- **Golden eval blind spot closing**: an `expect_miss` query ("how much
  does it cost to run a job") now hits — promote it in
  `services/knowledge-mcp/eval/golden.yaml` (improvement, not failure).

## Pulse check (2026-09-09, session start)

Nothing new on `main` since #18 (2026-09-05); no open PRs. All 8 Flux
kustomizations Ready at `main`; Prometheus and Alertmanager healthy on their
claims (TSDB 1.3 GiB against the 8 GiB cap, four days after the recreate);
the direct nightly reingest fired every morning at 07:30Z with the index at
`main`; only `Watchdog` and the null-routed phyt-system job firing. Node
disk 75.8% by node-exporter's avail-based view (23.7 GiB free) — the
2026-09-05 recreate freed ~3.4 GiB by dropping the ephemeral TSDB copy.

Findings:

- **Two merges from 2026-09-03 were half-landed** (found by the build review
  below, still true at this pulse): #16's probe merged as code only — the
  pod and the manifest sat at 0.1.0 because CI never builds images — while
  its PrometheusRule half DID reconcile, so `JobsBridgeDown`'s text
  described a probe the running pod did not have; and #15's IAM identity
  merged as terraform only — the `aws-creds` Secret had not been written
  since 2025-01-14. Both closed this session (session log below).
- **Grafana's admin password is the chart default**, behind the LAN
  traefik ingress with the full Prometheus datasource. Fix: terraform
  module + SM entry + ESO policy ARN + prod values patch — agent PR now
  that the scoped ESO policy is the live identity.
- **Upgrade remediation** is set only on kube-prometheus-stack; a failed
  chart upgrade on harbor / jupyterhub / tailscale parks `apps` NotReady
  until a human acts, and cert-manager floats unpinned. Small agent-only PR.

## Session log — build review, then landing the half-landed merges (2026-09-03 → 09-09)

A whole-repo review (eight layer readers against a live read-only pulse,
four planners from opposed lenses, adversarial verification of the
load-bearing claims) set the order: close the two half-landed merges and
refresh this file; open the gateway spec in parallel; three narrow
hardening changes; then gateway slices. Landed since:

- **PR #17 — golden-eval reword (2026-09-05):** the one query naming a
  family location (a public-repo hard rule) reworded; recall@5 25/25, MRR
  0.923 before and after — retrieval-neutral. Forward fix, no history
  rewrite.
- **PR #18 — monitoring state on durable, size-capped claims (2026-09-05):**
  Prometheus 10 Gi with `retentionSize` 8 GiB, Alertmanager 1 Gi, both
  `local-path`, prod patch only. Not the NAS (Prometheus does not support
  its TSDB on NFS) and not Harbor (a registry). Adds no disk — emptyDir
  already lived on the node fs. Operator decision: existing history
  dropped. Landed and verified on the cluster within a minute of merge.
- **ESO IAM cutover executed (2026-09-09, operator, per
  `docs/runbooks/external-secrets-iam.md`):** targeted apply 4 add / 0
  change / 0 destroy (13 secret ARNs); `aws-creds` swapped; all 13
  ExternalSecrets re-synced on the new read-only identity. Verified first:
  `aws-creds` held the legacy user's key and the phyt tenant's store uses a
  different one. The legacy account-wide key stays ACTIVE until step 5
  (deactivate → cool-down → delete).
- **PR #19 — jobs-mcp 0.2.0 (opened 2026-09-09, CI green):** the #16 probe
  finally ships (image pushed, contents verified), pod hardening parity
  with knowledge-mcp (no SA token, seccomp, read-only rootfs, no caps,
  64Mi /tmp), and a PR-only CI guard that fails an image-input change
  without a version + tag bump — the drift that left #16 undeployed.

## Session log — gateway slice 1 (2026-09-09 → 09-10)

Spec → code in a fresh session with the spec as the single input, then the
house adversarial review, then merge:

- **PR #22 — gateway slice 1, MERGED 2026-09-10 (squash `a7a7544`).**
  `services/gateway/` in Python (SIGN-OFF 1): the §1 ledger (integer µUSD,
  one `sqlite3` connection with `isolation_level=None`, one mutex, explicit
  `BEGIN IMMEDIATE`, compare-and-add on `scope_totals`, boot recompute, clock
  guard, job-cap pin, brake latch), §4 registry admission, the §3 OpenAI
  subset (reject-by-name; sampling params stripped and declared; buffered
  `stream`), the metered client behind an interface with a fake, the lane
  state machine, probes, metrics, `gateway-smoke-local`. 224 tests in ~4 s
  including the §14 property tests (hypothesis stateful runs, N parallel
  reserves against `cap = k·w`, a crash injected between every pair of SQL
  statements followed by restart + sweep). CI: ruff, mypy --strict, pytest,
  the smoke, `uv lock --check`, and the PR-only version-bump guard ported to
  `pyproject.toml` (its manifest-tag half arms when slice 2 lands the
  Deployment). Nothing reconciles or spends.
- **Review: three lenses** (money red-team, Python/async runtime critic,
  spec-conformance + identity + CI) — 3 HIGH-class findings (two reviewers
  converged on each), 10 MED, ~20 LOW; every HIGH and MED fixed with a test
  before merge. Headline fixes: a client disconnect on the buffered stream
  could strand the reservation until the next boot sweep (the money path now
  runs as a detached, shielded task; the abort on a real cancellation is a
  synchronous write); connect-phase timeouts settled at the full reservation
  and never counted toward lane-down (per-call connect budget; a connect
  timeout releases like a refused connection); refusals still shipped the
  prompt to `count_tokens` (a read-only pre-admission on a 32-token floor runs
  first — cap 0 makes zero provider calls). Also: a post-call ledger failure
  is a non-retryable 503; the boot sweep settles every `reserved` row and
  runs after `quick_check`, with boot failures going to readiness instead of
  a crash loop; `403 billing_error` is the spend limit, not an auth flap;
  in-stream `error` events are classified by type; the class table is one
  ASGI middleware. As-built deltas are recorded at the top of the spec.
- **Judgment calls recorded in the PR** (all reversible): an optional
  per-caller `max_day_billed_usd` behind the `caller_day` scope; the
  request-cap ceiling is the tightest of `MAX_REQUEST_CAP_USD`, the caller's
  and the project's `max_request_cap_usd`; `E_NOT_FOUND` (404) joins the
  taxonomy; a client disconnect settles from usage (≤ the reservation)
  rather than at it.
- **LiteLLM considered and declined for v1** (decisions log).
- **Slice 2 owes the spec's `(verify)` items on the first live call:**
  whether `count_tokens` counts `output_config` grammar tokens (the byte
  heuristic now counts a `json_schema`), the 429-without-`retry-after`
  spend-limit shape, `usage.inference_geo`; plus
  `terminationGracePeriodSeconds` ≥ 30 (uvicorn's graceful shutdown is 25 s)
  and parsing the deployed registry in CI with the manifest's
  `MAX_REQUEST_CAP_USD`.
- No cluster changes this session; no pulse taken (agent-only work).

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
  public-site downtime, then untargeted applies are clean again. Live read
  2026-09-03: the public-ports resource has real content drift beyond the
  schema drift (ports changed outside terraform, one of them documented
  nowhere), so stage 1 — reconcile the ports resource to reality and ignore
  the irreproducible `user_data` — comes before any replacement window.
- **~~Harbor UI on the tailnet~~ — DONE 2026-09-01** (`harbor-ui` Ingress
  via the tailscale operator). The registry *hostname* stays
  `harbor.internal` — renaming it is a real migration (image refs, pull
  secrets, containerd trust, CA SANs, build scripts) with no forcing event.
- **Build-order item 1 (storage classes + site labels)** — deferred again
  2026-09-03 with reopen triggers (decisions log): one node, one
  provisioner, no scheduling effect until a second node exists.
- **Legacy ESO key deactivation** (operator, runbook step 5): deactivate
  after a cool-down from the 2026-09-09 cutover, then delete the key, detach
  `SecretsManagerReadWrite`, delete the console user and the local backup.
- **Grafana admin credential** onto the secret path (chart default today)
  and **upgrade remediation** on the three HelmReleases that lack it +
  cert-manager pin — agent-only PRs queued (pulse 2026-09-09).

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

- **Gateway slice 2 — secrets, Harbor, manifests, runbook, first live call**
  (spec §7, §8 MERGE GATE, §13, §14). Slice 1 is merged (PR #22) and deployed
  nowhere. **PR #24 is open (draft) with all of it, three-lens reviewed; the
  MERGE GATE steps below are the operator's, in `docs/runbooks/gateway.md`.** The order is load-bearing, one reconciliation-chain change per
  window: (1) ESO reader identity — done 2026-09-09; (2) OPERATOR: Console
  workspace `homelab-gateway` with a monthly spend limit set BEFORE the key
  is minted (set it to $1 first for the deliberate trip, raise afterwards),
  the workspace-scoped key straight into `terraform.tfvars`; (3) three SM
  entries as terraform modules + their `.secret_arn` in
  `terraform/iam-external-secrets.tf` in the SAME PR — targeted plan expected
  **6 add / 1 change / 0 destroy** (the policy changes in place; anything to
  destroy means STOP), one SSO login for the whole build; (4) OPERATOR:
  Harbor project `gateway` + pull robot (secret into tfvars) + the image
  pushed from the `desktop-linux` builder at the manifest's tag (`0.1.1` — moved by the slice-2 review);
  (5) `apps/base/gateway/registry.yaml` from
  `services/gateway/registry.example.yaml` with the operator's caps, passing
  CI incl. Σ caps ≤ the attested workspace limit; (6) no tailnet node named
  `gateway`; (7) merge, watch Flux, verify on the cluster (pod on 0.1.1,
  both ExternalSecrets synced, `/readyz` 200 over the tailnet, the §9 rules
  loaded). Then the first live operator call from the workbench: `haiku`,
  `max_tokens: 5`, cap `0.01` ⇒ `X-Gateway-Lane-Used: metered`, non-zero
  `X-Gateway-Billed-USD`, the row in `/ledger/requests`,
  `gateway_billed_usd_total > 0` scraped; cap `0` ⇒ `402`; the deliberate $1
  workspace-limit trip ⇒ `503` + alert; then raise the limit. §13 checks 1,
  2, 7, 10, 14, 15 land in `docs/runbooks/gateway.md`. STATUS +
  `mini/mcp-config.md` lines after. Slice 3 (`gateway-smoke` task type, the
  jobs-mcp §11 proof) follows only once slice 2 is live.
- ~~Merge PR #19~~ merged and verified live 2026-09-09: pod on 0.2.0, the
  hardening block applied, the probe in the running bundle — #16's loop is
  closed on the cluster, not only in git.
- **Operator step pending:** deactivate the legacy ESO key (runbook step 5)
  after a cool-down; then delete it and the local `aws-creds` backup.
- **Agent-only PRs queued:** Grafana admin credential onto the secret path;
  upgrade remediation on harbor / jupyterhub / tailscale + cert-manager pin;
  a knowledge-mcp version-bump guard mirroring #19's.
- **Still parked:** build-order item 1 (reopen triggers in the decisions
  log), the dead-man's snitch on Watchdog, the Alertmanager NetworkPolicy,
  Lightsail drift stage 1, node disk headroom. Per-caller jobs-mcp tokens
  (the NanoClaw retrofit) remain what re-arm the queue-shaped nightly — and
  the retrofit is larger than spec §2's "small change to the auth check"
  (no caller column, no ownership on cancel/status, a global UNIQUE on
  `idempotency_key`); it gets its own short spec when NanoClaw is next.

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
- 2026-09-03: the fourth knowledge alert `KnowledgeIndexNeverBuilt` is KEPT.
- 2026-09-03: build-order item 1 (storage classes + site labels) deferred
  again — one node, one provisioner (`local-path`), no CSI: labels change
  no scheduling decision and a NAS-backed class means a new driver with no
  consumer. Reopen when an edgepve k3s agent joins, or a NAS-backed class
  gets a real consumer.
- 2026-09-05: Prometheus and Alertmanager state moves to `local-path`
  claims with `retentionSize` as the real cap (local-path does not enforce
  claim sizes) — not the NAS (Prometheus does not support its TSDB on NFS)
  and not Harbor (a registry). The existing history was dropped, not
  exported (operator).
- 2026-09-09: external-secrets authenticates as the terraform-managed
  read-only `k3s-external-secrets-reader` (cutover per runbook, 4 add / 0 /
  0, all 13 ExternalSecrets re-synced). Every new Secrets Manager entry an
  ExternalSecret reads must add its module's `.secret_arn` to
  `terraform/iam-external-secrets.tf` in the same PR, or ESO gets
  AccessDenied for it. Legacy key deactivation follows a cool-down.
- 2026-09-09: "merged ≠ landed" — CI never builds images and terraform-only
  PRs change nothing live, so a merge is verified on the cluster, not on
  GitHub. A jobs-mcp PR that changes image inputs must move the package
  version, the manifest tag and the advertised MCP server version together
  (CI guard in #19); knowledge-mcp gets the same guard at its next bump.
- 2026-09-09: Python is the house language for every NEW service, starting
  with the gateway; jobs-mcp and knowledge-mcp stay TypeScript (a rewrite,
  if ever, is all-at-once, never piecemeal). The two-toolchain cost is
  recorded in `docs/specs/gateway.md` §12.
- 2026-09-09: the gateway's budget cap is a bound on LIST-equivalent cost on
  every lane (`0` = no model call anywhere), and jobs-mcp's `spent_usd`
  reports list-equivalent USD — the same unit as `budget_cap`.
- 2026-09-09: the subscription lane (headless Claude on `worker-01` under
  the operator's plan) is DEFERRED — the operator holds off on using the
  subscription for automated jobs. Gateway v1 is metered-only; the
  pull-agent design stays in the spec as the seam; reopening is an explicit
  operator decision, not a tripwire.
- 2026-09-10: gateway slice 1 merged (PR #22) with the as-built deltas the
  review forced, recorded at the top of the spec: a client disconnect never
  cancels a provider call in flight (the call completes and settles from its
  usage, ≤ the reservation; only a pod shutdown mid-call settles AT the
  reservation as `aborted`); the boot sweep settles every `reserved` row
  regardless of timestamp (a stale-RTC orphan is still an orphan);
  connect-phase timeouts release like a refused connection and count toward
  lane-down, only read timeouts settle at the reservation; refusals never
  reach `count_tokens` (a count-independent pre-admission runs first).
- 2026-09-10: LiteLLM considered as a replacement for the gateway and
  DECLINED for v1: its budgets are settle-at-end (a $0.01 remaining budget
  admits a $5 call; N concurrent requests jointly overshoot — the model §15
  rejects by name), it has no per-request budget cap (job caps would mean the
  executor minting virtual keys with a management credential), it needs
  Postgres for any budget feature (reversing SIGN-OFF 4 through the back
  door), its router retries/fallbacks are default-on, and prompt storage is a
  setting rather than a schema fact. Reopen at the §11 tripwires (tools,
  token streaming, images, a second provider, a local lane): the likely shape
  then is the gateway's ledger and admission IN FRONT of LiteLLM as the
  translation layer, never LiteLLM alone.
- 2026-09-10: `services/gateway/` is the Python precedent the next service
  copies (the six house idioms ported once: loud env validation, caller-map
  parse + constant-time auth, healthz/readyz split, zero-filled series,
  additive migrations, one-line JSON logs) plus two async lessons the TS
  review corpus did not carry: Starlette cancels a streaming response's
  generator on client disconnect and anyio re-delivers `CancelledError` at
  every await, so money paths run as detached shielded tasks and aborts write
  synchronously; a scalar SDK timeout makes the connect phase as long as the
  read phase, so every provider call carries `httpx2.Timeout(total, connect=10)`.
