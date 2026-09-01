# Host: edgepve — hot-edge Proxmox (Topton N150)

Edge-site Proxmox host: Topton N150, 16GB DDR5, 500GB NVMe, 4x 2.5GbE
(Intel i226, driver `igc`) + 2x SFP+ 10G (Intel, driver `ixgbe`).
Committed first tenant: the edge gateway
LXC (`tailscale-gw.md`, already parameterized for this build). Candidate
later roles per `docs/STATUS.md` (watchdog, second Pi-hole, edge worker).

**Network design:** one 2.5G port is the dedicated management plane
(`vmbr0`, untagged); the 10G port is a VLAN-aware guest trunk (`vmbr1`).
This mirrors the compute-site host (dellpve): management stays reachable
regardless of guest/trunk churn, and guests get the 10G path to the site
backbone (and the NVMe store) without touching the management NIC. The
host is **not** the site router — WAN and inter-VLAN routing live upstream.

## Installer choices (record of what was picked)

- Management Interface: `nic0` (`igc`, 2.5G) — installer builds `vmbr0` on it.
- **Pin network interface names: ON** → stable `nicN` names via systemd
  .link files; NIC names below assume this.
- Hostname: `edgepve.<domain>` — match the FQDN domain of the other PVE hosts.
- Management IP/gateway/DNS: on the management subnet (values kept out of
  this public repo; see parameters below).
- Filesystem: installer default — ext4 root (96G) + LVM-thin `data`
  (~338G) for guests, 8G swap. No ZFS → no ARC cap needed at 16G RAM.
- Installed 2026-09-01: PVE 9.2.2 (Debian trixie).

## Parameters

| Placeholder | Meaning | Example shape |
|---|---|---|
| `<MGMT_IP>/<PREFIX>` | host address on the mgmt subnet | `x.x.x.2/24` |
| `<MGMT_GW>` | mgmt subnet router (must exist & route — host egress for apt) | `x.x.x.1` |
| `<VID>` | a guest VLAN id on the trunk | `100` |

NIC map (discovered 2026-09-01; names pinned by the installer):

| NIC | Hardware | Role |
|---|---|---|
| `nic0` | igc 2.5G | management (vmbr0, untagged) |
| `nic1`–`nic3` | igc 2.5G | spare |
| `nic4` | ixgbe SFP+ 10G | guest trunk (vmbr1) |
| `nic5` | ixgbe SFP+ 10G | spare (second trunk / LACP later) |

Re-identify ports any time (every `igc` is a 2.5G port, the 10G shows a
different driver):

```bash
for n in /sys/class/net/nic*; do n=${n##*/}; echo "$n $(ethtool -i $n | awk '/driver/{print $2}') $(cat /sys/class/net/$n/carrier 2>/dev/null)"; done
```

## /etc/network/interfaces (target)

The installer writes the `vmbr0` half; only the trunk half is added
post-install. Wrong `bridge-ports` on vmbr0 (cable in a different igc
port) is a one-line fix here.

```
auto lo
iface lo inet loopback

# management — dedicated 2.5G port, untagged access port on the mgmt subnet
auto nic0
iface nic0 inet manual

auto vmbr0
iface vmbr0 inet static
	address <MGMT_IP>/<PREFIX>
	gateway <MGMT_GW>
	bridge-ports nic0
	bridge-stp off
	bridge-fd 0

# guest trunk — 10G port, VLAN-aware, no host IP; switch side = trunk port
auto <NIC_10G>
iface <NIC_10G> inet manual

auto vmbr1
iface vmbr1 inet manual
	bridge-ports <NIC_10G>
	bridge-stp off
	bridge-fd 0
	bridge-vlan-aware yes
	bridge-vids 2-4094

source /etc/network/interfaces.d/*
```

Apply with a console available (JetKVM on the rack reaches this box —
plug its HDMI/USB here during network changes), not over the same link
being changed:

```bash
ifreload -a
```

Verify: `ip -br link` (nic0 and the 10G both UP), `bridge vlan show`
(trunk vids present on `<NIC_10G>`), `ping <MGMT_GW>`, `apt update`
succeeds, UI at `https://<MGMT_IP>:8006`.

## Post-install (applied 2026-09-01)

1. Repos (PVE 9 deb822): `Enabled: false` prepended into
   `pve-enterprise.sources` and `ceph.sources`; wrote
   `/etc/apt/sources.list.d/proxmox.sources` with component
   `pve-no-subscription` for the os-release codename, reusing the
   enterprise file's `Signed-By` keyring path.
2. `apt full-upgrade` (9.2.2 → 9.2.11, new kernel), then reboot.
3. Trunk stanza added, `ifreload -a` — vmbr0 untouched by the reload —
   then the reboot doubled as the restart-safety proof: both bridges and
   the full VID range came back on their own. Pre-change backup on the
   host: `/etc/network/interfaces.bak-preinstall`.

## Guest wiring

- Default: guests attach to `vmbr1` with a per-NIC `tag=<VID>` (VLAN-aware
  pattern, same as dellpve).
- A guest that must not see tags gets the dellpve per-VLAN-bridge pattern:
  `vmbr1.<VID>` + a dedicated `vmbr<VID>` bridging it.
- **First tenant — edge gateway LXC** (`tailscale-gw.md`): single leg on
  `vmbr1` with the tag of the subnet it advertises — WAN here is 5Gb, so
  exit-node throughput can exceed the 2.5G management port; don't put the
  gateway's data path on vmbr0. Decided 2026-09-01: **no mgmt leg, ever**
  — the gateway advertises service subnets only. Remote reach to this
  host's UI/SSH is the host's own tailscale node, matching dellpve/naspve;
  that keeps the mgmt VLAN off every guest path and doesn't route host
  management through a guest this same host runs. **Built 2026-09-01 as
  CT 101** (`tailscale-gw-edge`): single leg on vmbr1 tagged into the
  servers VLAN, advertising that subnet + exit node; key expiry off.
- Later k3s edge capacity attaches to `vmbr1` (service VLAN tag); site
  labels (`site=edge`) are a k8s-layer concern, not this file's.

## Extensions (documented, not applied)

- Host IP on a trunk VLAN — `vmbr1.<VID>` stanza with an address (dellpve
  does this) — only if the *host* ever needs a 10G path, e.g. backup
  target on the edge NVMe store.
- Jumbo frames: leave MTU 1500 unless raised deliberately end-to-end
  (switch + every peer on the path); mixed MTU fails silently.

## Notes / known drift

- 2026-09-01: host joined the tailnet as a plain node (`edgepve`, key
  expiry disabled, no routes advertised) — the remote UI/SSH path
  (`https://edgepve:8006`). The mgmt VLAN is deliberately not advertised
  to the tailnet by anything.
- Switch-side requirements (any vendor, learned the hard way at build
  time): the mgmt VLAN must be the **native/primary** network on the mgmt
  access port, **tagged on every inter-switch uplink hop** between router
  and that switch, and deliberately absent from the 10G trunk port. On
  UniFi specifically: a saved Ethernet Port Profile overrides per-port
  settings, and uplinks restricted to "Custom" tagged lists silently drop
  new VLANs.
- dellpve runs PVE 8.4; this box installs from a newer ISO. Version skew
  between standalone hosts is fine (no cluster, no corosync — a second
  edge node would revisit that, over the mgmt NIC).
- Single-node install: re-IPing management later is cheap
  (`/etc/network/interfaces` + `/etc/hosts`), so don't block rack
  integration on final VLAN design — the trunk carries whatever VLANs the
  switch grows.
