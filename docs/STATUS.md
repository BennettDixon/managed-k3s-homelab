# STATUS

Rolling status of the personal-cloud buildout. Updated at the end of every working
session. Tailnet MagicDNS names only — no LAN IPs or site details in this file.

_Last updated: 2026-08-27_

## Standing infrastructure

- **k3s cluster** — single node (`k3s`), managed by Flux from `main` of this repo.
  Running: harbor (private registry, homelab root CA via cert-manager), jupyterhub,
  kube-prometheus-stack, personal-site, tailscale-operator (kubectl over tailnet).
- **Tailnet** is the only network; nothing is publicly exposed except the
  Lightsail proxy path for the personal site.
- **Gateways (HA pair, compute site):** `tailscale-gw` and `tailscale-gw2`, each
  advertising the server subnet + exit node; Tailscale fails over between them.
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
- **Proxmox hosts on tailnet:** `dellpve` (compute), `naspve` (storage/NAS).
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
- **Delete the smoke-test cron** after 2026-08-27: `/etc/cron.d/worker-smoke-today`
  on `worker-01` (day/month-restricted; would recur yearly if left).
- **After any TrueNAS upgrade:** re-run the tailscale package install on
  `truenas-bulk-52tb` (`proxmox/nas-vm.md`); the rc tunable survives.

## Workbench (2026-08-31)

`agent-mini` is live on the rack with JetKVM out-of-band console
(`jetkvm-hot-edge`). Bootstrapped no-sudo: Claude Code, node LTS, kubectl,
n8n MCP (connected), kubeconfig, and its own SSH key trusted by `dellpve`,
`naspve`, `truenas-bulk-52tb`. Remaining interactive steps in
`mini/setup.md`.

## Parked (deliberate, not forgotten)

- **Edge-site Proxmox box (N150, 16GB DDR5, 500GB NVMe) — rack integration
  unfinished.** Its committed role: the edge gateway LXC pair member
  (`proxmox/tailscale-gw.md` is already parameterized for this build) —
  though note the workbench currently covers edge exit-node duty. Beyond
  that it's expected to be underused; candidate roles to weigh later:
  cross-site watchdog (edge node probing compute-site services and
  alerting, and vice versa), second DNS/Pi-hole for edge resilience,
  or a small edge worker for latency-sensitive jobs once jobs-mcp exists.
  No commitment yet — revisit after jobs-mcp v1.

## Next session starts with

- ~~First n8n routine~~ **done 2026-08-27 (same day):** `smoke-heartbeat`
  created/activated/executed entirely via the n8n REST API; canonical export
  in `n8n/smoke-heartbeat.json`.
- The **jobs MCP spec** (`enqueue`/`status`/`artifacts` envelope, budget_cap
  mandatory) — design-first, before manifests.
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
