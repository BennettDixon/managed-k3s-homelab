# worker smoke test

**Purpose:** prove the subscription lane end-to-end — a worker LXC runs a
trivial headless `claude -p` task and lands the JSON result + log on the bulk
NAS, addressed by tailnet name.

**Interface:** `smoke.sh` (no args). Exit 0 iff the Claude call succeeded and
the artifact shipped. Artifacts land at
`truenas-bulk-52tb:/mnt/BulkPoolZ2/artifacts/worker-smoke/<UTC timestamp>/`.

**Transport decision:** scp over the tailnet, not an NFS mount — it needs
nothing but the SSH service already enabled on the NAS (no export config, no
fstab, nothing to wedge a reboot), which is the simplest thing that works.
Revisit if artifact volume ever makes per-file scp a bottleneck.

**Dependencies:** Claude Code + Node on the worker; `/etc/worker/claude.env`
with `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, root-only, never
committed); worker SSH key trusted by the NAS root user (installed via the
TrueNAS middleware API, see `proxmox/nas-vm.md`).

**Where data lives:** everything durable is on the NAS dataset
`BulkPoolZ2/artifacts`; the worker keeps nothing (staging dir is a wiped
tmpdir).

**Future (jobs MCP era):** replace root-scp with a dedicated NAS user and a
restricted key; route model calls through the gateway with a budget_cap
instead of calling the subscription lane directly.
