# Workbench setup — `agent-mini`

Always-on Mac mini on the rack, out-of-band console via JetKVM
(`jetkvm-hot-edge` on the tailnet). The workbench is the trusted tier: it
drives the homelab over the tailnet, runs Claude Code on the subscription
lane, and (per the jobs-mcp spec) builds/pushes images to Harbor.

Everything below installs **without admin rights** into the user account;
the only sudo-gated item is the Xcode CLT (GUI dialog, one click).

## Base state (2026-08-31)

- macOS 26.x, arm64, sleep disabled at powerd level (rack-safe by default).
- Toolchain in `~/.local/bin` (PATH added via `~/.zshenv` so non-interactive
  SSH sessions get it too): `claude` (native installer, self-contained —
  needs no node), `node`/`npm`/`npx` (official darwin-arm64 tarball extracted
  to `~/.local/node-v24*` — nvm refuses to install without Xcode CLT, the
  tarball needs nothing), `kubectl` (direct binary).
- Xcode CLT required only for `git`; `xcode-select --install` pops a GUI
  dialog — click it on the JetKVM console.

## Access the workbench holds

- `~/.ssh/id_ed25519` (`agent-mini-workbench`) — trusted by `root@dellpve`,
  `root@naspve` (plain `authorized_keys`), and `root@truenas-bulk-52tb`
  (via TrueNAS middleware `user.update`, the only way that persists —
  see `proxmox/nas-vm.md`).
- `~/.kube/config` — tailscale-operator context; `kubectl get nodes` works
  over the tailnet with no VPN/subnet dependency.
- `~/.zshenv` — `N8N_API_KEY` (never committed; rotate in the n8n UI, update
  here).

## MCP

n8n MCP registered at user scope exactly as `mcp-config.md` documents:

```bash
claude mcp add --scope user n8n \
  --env N8N_API_URL=http://n8n:5678 \
  --env 'N8N_API_KEY=${N8N_API_KEY}' \
  -- npx -y n8n-mcp
```

The `${N8N_API_KEY}` reference expands from the environment at MCP startup
(verified: `claude mcp list` shows Connected). Config lands in
`~/.claude.json`; no secret is stored in it.

## Account tiering

Two local accounts, deliberately scoped (NanoClaw belongs to neither — per
the harness rules it will be a sandboxed container with jobs+knowledge MCP
access only, most likely a cluster pod behind a NetworkPolicy, and it cannot
exist before those MCPs do):

| Tier | Account | Holds |
|------|---------|-------|
| Human admin | `bennett` (admin) | full-access homelab key, kubeconfig, repo push, interactive Claude |
| Trusted headless runtime | `agent` (standard) | own Claude login, n8n MCP + `N8N_API_KEY`, runtime SSH key trusted **nowhere** until a task needs it |

The `agent` account mirrors the no-sudo toolchain (`claude`, node tarball,
PATH via `~/.zshenv`), has its own repo clone, and — important — a manually
copied `CLAUDE.md`: the harness file is gitignored, so every fresh clone
needs it copied in by hand or the runtime works without the ground rules.

## Remaining operator steps (interactive, one-time)

Done 2026-08-31: Xcode CLT installed; `claude` logged in on both accounts;
`agent` verified end-to-end (headless `claude -p` returns clean JSON);
`agent` has the repo clone. Still open:

1. `bennett`'s own repo clone whenever needed (public https; remember the
   CLAUDE.md hand-copy; add a push credential only when the workbench
   needs to commit).
2. Later, when the workbench builds images: Docker + the Harbor root CA
   trust (both need admin; see the harbor CA runbook/memory).

Admin-console state (verified 2026-08-31): key expiry disabled on
`agent-mini` and `jetkvm-hot-edge`; stale `admins-mac-mini` node deleted;
`agent-mini` intentionally offers exit node. (The JetKVM's tailnet name is
DNS-level — its OS hostname is the factory `jetkvm-<id>`.)

macOS quirk for scripts: there is no `timeout` command in the base system —
worker recipes written for Debian need that flag dropped or replaced here.
