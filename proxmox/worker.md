# Recipe: worker LXC (subscription-lane Claude executor)

Debian LXC that runs headless Claude Code tasks on the subscription lane.
First instance: `worker-01` (VMID 120, compute-site Proxmox host, 2 cores /
4G RAM / 8G rootfs on `vmstore`).

Workers are plain tailnet nodes today; tagged tailnet identities with scoped
MCP access come with the jobs-MCP era (see agent-stack build order).

## Create (on the Proxmox host)

Same base pattern as the gateway recipe (`tailscale-gw.md`): unprivileged
Debian 12 CT with nesting + TUN passthrough, static IP on the server subnet,
`onboot=1`, public DNS bootstrap resolvers. Differences: 2 cores, 4096 MB
RAM, 1024 MB swap, 8G rootfs, no route advertisement.

```bash
pct create <VMID> local:vztmpl/<TEMPLATE> \
  --hostname worker-NN --unprivileged 1 --ostype debian \
  --cores 2 --memory 4096 --swap 1024 --features nesting=1 \
  --net0 name=eth0,bridge=<BRIDGE>,firewall=1,gw=<LAN_GW>,ip=<CT_IP><VLAN_OPT> \
  --nameserver "1.1.1.1 9.9.9.9" \
  --rootfs <STORAGE>:8 --onboot 1
# + the two TUN lines in /etc/pve/lxc/<VMID>.conf (see tailscale-gw.md)
```

## Inside the container

```bash
apt-get update && apt-get install -y curl ca-certificates git tmux gnupg

# Node current LTS via NodeSource
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg
echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
  > /etc/apt/sources.list.d/nodesource.list
apt-get update && apt-get install -y nodejs

npm install -g @anthropic-ai/claude-code
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up          # plain node; auth as owner, disable key expiry
```

## Subscription-lane auth (manual, operator)

`claude setup-token` **must run interactively** (OAuth in a browser) and does
NOT log the machine in — it prints a long-lived token. Store it root-only:

```bash
mkdir -p /etc/worker
read -rs TOKEN   # paste token (silent), press Enter
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" > /etc/worker/claude.env
chmod 600 /etc/worker/claude.env
unset TOKEN
```

Sanity check: `wc -c /etc/worker/claude.env` should be well over 30 bytes —
a 25-byte file means the paste didn't register and the value is empty.

## Artifact path to the NAS

The worker gets its own ed25519 key (`/root/.ssh/id_ed25519`); its public key
is added to the NAS root user **via the TrueNAS middleware API** (direct
`authorized_keys` edits are regenerated away — see `nas-vm.md`). Uploads go
by tailnet name: `truenas-bulk-52tb:/mnt/BulkPoolZ2/artifacts/...`.

## Smoke test

`workers/smoke/smoke.sh` in the repo is canonical; deployed to
`/opt/worker/smoke.sh`. See `workers/smoke/README.md` for interface and the
scp-over-NFS transport decision. The kickoff-day cron
(`/etc/cron.d/worker-smoke-today`) is day/month-restricted to fire hourly on
2026-08-27 only — delete the file after that date (otherwise it recurs
yearly on that day).
