# NAS VM — `truenas-bulk-52tb`

TrueNAS Core VM on the storage-site Proxmox host (`naspve`, VMID 100). Fronts
the bulk/archive tier: ZFS pool `BulkPoolZ2` (~52T raw, RAIDZ2, HDD
passthrough). Runs SMB/NFS shares, plex media, and iocage jails.

## Tailnet identity — the rule

The VM is a first-class tailnet node: **`truenas-bulk-52tb`** (plain node — no
subnet advertisement, no exit node; joined 2026-08-27, key expiry disabled).

**Anything durable that targets this NAS uses the tailnet name** — replication
targets, NFS exports/mounts, scp destinations, backup jobs. Never its LAN
address: LAN reachability from other sites depends on which subnet-router
gateway is primary at that moment, while the tailnet name is stable and
direct.

Naming scheme (there are several TrueNAS instances in the fleet): the tailnet
name encodes tier and capacity — `truenas-bulk-52tb` is the bulk HDD tier;
the hot-edge NVMe instance and the small SSD-pool instance (compute site,
`TruenasCoreSSD` VM on dellpve) get equivalent names when they join.

## How Tailscale is installed (TrueNAS Core 13 / FreeBSD 13.1)

TrueNAS Core is an appliance: `pkg` repos are disabled by default and the OS
image is replaced wholesale on upgrades. The install therefore deviates from
a vanilla FreeBSD box:

```sh
# 1. temporarily enable the FreeBSD pkg repo
sed -i '' 's/enabled: no/enabled: yes/' /usr/local/etc/pkg/repos/FreeBSD.conf

# 2. install (IGNORE_OSVERSION: the FreeBSD:13 repo is built against a newer
#    13.x than the TrueNAS base). Expect pkg to upgrade itself first — the
#    bundled pkg (1.17) cannot read the modern catalogue format.
env IGNORE_OSVERSION=yes pkg update -f -r FreeBSD
env IGNORE_OSVERSION=yes pkg install -y -r FreeBSD tailscale

# 3. put the repo back the way the appliance ships
sed -i '' 's/enabled: yes/enabled: no/' /usr/local/etc/pkg/repos/FreeBSD.conf

# 4. persist tailscaled across reboots the TrueNAS way. Plain `sysrc` does NOT
#    survive: the middleware regenerates rc.conf at boot. Use a tunable:
midclt call tunable.create '{"var": "tailscaled_enable", "value": "YES", "type": "RC", "comment": "tailnet node - reinstall pkg tailscale after TrueNAS upgrades", "enabled": true}'

service tailscaled start
tailscale up        # auth as tailnet owner, then disable key expiry in console
```

## Appliance-state changes to know about

- `pkg` self-upgraded 1.17.5 → 2.6.2 during install (harmless; middleware
  does not use pkg).
- Tunable id 7 (`tailscaled_enable=YES`, type RC) exists in the TrueNAS
  config DB — visible under System → Tunables.
- **After any TrueNAS version upgrade:** the package is wiped with the old
  boot environment; re-run the install above (the tunable survives, so the
  service starts as soon as the package is back). Tailscale node identity
  lives in `/var/db/tailscale/` — if that path survives the upgrade the node
  keeps its identity; otherwise re-auth.
- SSH service: enabled, key-only, root password login off. Automation key is
  the workbench's `id_ed25519_homelab_ops`.
