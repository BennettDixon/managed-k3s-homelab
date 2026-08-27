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
- **Gateways:** `tailscale-gw` (compute site) advertises the server subnet and
  offers exit node. A second HA gateway on the storage host is in progress
  (this session).
- **Proxmox hosts on tailnet:** `dellpve` (compute), `naspve` (storage/NAS).
- **Appliance tier (do not modify):** gateway LXCs, Pi-hole, NAS VM, storage
  pools, Tailscale ACLs.

## In flight — agent-stack kickoff session (2026-08-27)

Goal: first pieces of the agent stack at the compute site.

| Step | What | Status |
|------|------|--------|
| 1 | Second HA gateway LXC on `naspve` (+ `proxmox/tailscale-gw.md` recipe) | **done** — `tailscale-gw2` (CT 101), routes + exit approved, key expiry off; also fixed non-persistent forwarding sysctls on gw-01 |
| 2 | NAS VM joins tailnet (+ `proxmox/nas-vm.md`) | **done** — `truenas-bulk-52tb`, plain node, key expiry off, reachable by name; replication/NFS rule documented |
| 3 | `worker-01` LXC + subscription-lane smoke test (+ `proxmox/worker.md`, `workers/smoke/`) | pending |
| 4 | n8n LXC, tailnet-bound (+ `mini/mcp-config.md` entry) | pending |
| 5 | Wrap-up: this file, verification checklist | pending |

## Manual follow-ups needed (operator)

- Install the workbench automation SSH public key for `root@dellpve` and
  `root@naspve` (key: `~/.ssh/id_ed25519_homelab_ops.pub` on the workbench).
  Everything in this session is blocked on it.
- Later in session, when prompted: Tailscale auth URLs for new nodes, route/exit-node
  approval + key-expiry disable in the admin console, `claude setup-token` on
  `worker-01`, n8n first-run setup + API key.

## Next session starts with

- First n8n routine (deterministic executor bridge) and the jobs MCP spec
  (`enqueue`/`status`/`artifacts` envelope) — see build order in the agent
  harness notes.

## Decisions log

- 2026-08-27: repo is public, so the agent-harness file (CLAUDE.md) and session
  kickoff prompts stay local (gitignored) rather than committed — they contain
  context that the repo's own rules keep out of published docs. Flip by removing
  the .gitignore entries if the repo ever goes private.
