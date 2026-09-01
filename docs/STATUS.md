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

## Parked (deliberate, not forgotten)
- **knowledge-mcp vector store — candidates to weigh at spec time:**
  (a) Postgres + pgvector in an LXC on the edge N150, data on its local
  NVMe (one SQL surface for vectors+metadata, multi-client, backups to the
  bulk NAS; costs a real server on residential power — and it would give
  the N150 its primary role); (b) LanceDB on NVMe-tier files (zero server
  ops); (c) sqlite-vec (one storage engine across the stack). Shared
  constraint either way: batch embedding/ingest runs on compute-site
  workers; the edge only serves queries. Queue store is NOT in scope here —
  jobs-mcp stays SQLite-local per its spec.
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

## Next session starts with

- ~~First n8n routine~~ **done 2026-08-27 (same day):** `smoke-heartbeat`
  created/activated/executed entirely via the n8n REST API; canonical export
  in `n8n/smoke-heartbeat.json`.
- ~~The jobs MCP spec~~ **APPROVED 2026-08-31** (`docs/specs/jobs-mcp.md`) —
  next: slice 1, `services/jobs-mcp/` (server, dispatcher, SQLite, tests; no
  manifests). Operator prerequisites in spec §12 are needed before slice 2.
- Groundwork after that: k8s storage classes + site labels (build order item 1).
- Optional workbench wiring: n8n-mcp on the mini per `mini/mcp-config.md`.

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
