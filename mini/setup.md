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

## Remaining operator steps (interactive, one-time)

1. Click **Install** on the Xcode CLT dialog (JetKVM) → then
   `git clone https://github.com/BennettDixon/managed-k3s-homelab.git ~/projects/managed-k3s-homelab`
   (public repo, https, read-only; add a push credential only when the
   workbench needs to commit).
2. `claude` login for the subscription lane (interactive OAuth).
3. Admin console: delete the stale `admins-mac-mini` node entry; decide
   whether `agent-mini` should keep offering exit node; disable key expiry
   on `agent-mini` and `jetkvm-hot-edge`.
4. Later, when the workbench builds images: Docker + the Harbor root CA
   trust (both need admin; see the harbor CA runbook/memory).
