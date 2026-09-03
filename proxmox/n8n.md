# Recipe: n8n LXC (deterministic-routine runner)

Debian LXC running n8n natively (npm + systemd), bound to its **tailnet
address only** — the UI/API are unreachable from the LAN or anywhere
off-tailnet. First instance: VMID 121 on the compute-site Proxmox host,
2 cores / 2G RAM / 8G rootfs.

**Why native npm, not docker-in-LXC:** one less runtime layer to maintain and
debug in an unprivileged container (no docker daemon, no overlay quirks);
n8n officially supports the npm install, updates are `npm update -g n8n`
under the same systemd unit, and logs flow through journald like every other
service here.

## Create

Same base pattern as `tailscale-gw.md` (unprivileged Debian 12, nesting,
TUN passthrough lines, static server-subnet IP, `onboot=1`), with 2 cores /
2048 MB / 8G rootfs. Then inside:

```bash
apt-get update && apt-get install -y curl ca-certificates gnupg build-essential python3
# Node current LTS via NodeSource (see worker.md for the repo lines)
apt-get install -y nodejs
npm install -g n8n
curl -fsSL https://tailscale.com/install.sh | sh
useradd -r -m -d /var/lib/n8n -s /usr/sbin/nologin n8n
tailscale up        # plain node; auth as owner, disable key expiry
```

## Tailnet-only binding

`/etc/n8n/n8n.env` (filled in after tailscale auth assigns the node its
address):

```
N8N_LISTEN_ADDRESS=<tailscale 100.x address of this node>
N8N_PORT=5678
N8N_SECURE_COOKIE=false
N8N_EDITOR_BASE_URL=http://n8n:5678/
WEBHOOK_URL=http://n8n:5678/
N8N_BLOCK_ENV_ACCESS_IN_NODE=false
JOBS_WEBHOOK_SECRET=<from terraform.tfvars jobs_mcp_webhook_secret>
ALERTS_WEBHOOK_SECRET=<from terraform.tfvars alerts_webhook_secret>
ALERTS_TELEGRAM_CHAT_ID=<numeric chat id of the operator's Telegram — never in git>
KNOWLEDGE_REINGEST_TOKEN=<from terraform.tfvars knowledge_mcp_n8n_reingest_token>
# JOBS_MCP_BEARER_TOKEN=<ONLY the future per-caller n8n-nightly token, never the v1 operator bearer; set when the queue-shaped nightly is re-armed — docs/runbooks/knowledge-mcp.md>
```

`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` is deliberate (it matches the n8n
default, but the posture deserves stating): workflow Code nodes read `$env`
to verify webhook shared secrets. The flip side: anyone who can author or
edit a workflow here — the operator, the workbench API key, and the
cluster-synced jobs-mcp API key — can read this whole env file, both
webhook secrets included. One trust domain, on purpose.

`N8N_SECURE_COOKIE=false` because the UI is served over plain HTTP; the
transport is the tailnet (WireGuard) — TLS here would be theater against the
threat model, and the listen address means there is no non-tailnet path.

`/etc/systemd/system/n8n.service`:

```ini
[Unit]
Description=n8n workflow runner
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=n8n
EnvironmentFile=/etc/n8n/n8n.env
ExecStart=/usr/bin/n8n
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now n8n
```

Note: binding to the tailscale address requires tailscaled to be up first —
hence the `After=tailscaled.service` ordering; `Restart=on-failure` covers
the race on slow boots.

## First run (manual, operator)

Open `http://n8n:5678` from any tailnet device: create the owner account,
then Settings → n8n API → create an API key for the workbench (goes into the
workbench env, see `mini/mcp-config.md` — never into this repo).

## Data

Workflows/credentials DB is SQLite under `/var/lib/n8n/.n8n/` (the service
user's home). Include it in any future backup routine; treat exported
workflow JSONs in `n8n/` (repo) as the canonical source once that directory
exists.
