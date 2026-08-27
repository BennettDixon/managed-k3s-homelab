# Recipe: Tailscale subnet-router / exit-node gateway LXC

Unprivileged Debian LXC that advertises the local server subnet to the tailnet
and offers itself as an exit node. One per site minimum; two per site for HA
(Tailscale automatically fails over between subnet routers advertising the
same routes).

Built twice so far (compute site: `tailscale-gw` on dellpve, `tailscale-gw2`
on naspve). Parameterized for reuse at other sites.

## Parameters

| Placeholder | Meaning | Example shape |
|---|---|---|
| `<VMID>` | Proxmox CT ID, free on the target host | `101` |
| `<NAME>` | tailnet hostname, short and boring | `tailscale-gw2` |
| `<BRIDGE>` | host bridge that reaches the server subnet | `vmbr0` / `vmbr1` |
| `<VLAN_OPT>` | `,tag=<VID>` if the bridge is VLAN-aware, empty if the bridge is untagged into the subnet | `,tag=100` or empty |
| `<CT_IP>` | free static IP on the server subnet, CIDR notation | `x.x.x.x/24` |
| `<LAN_GW>` | the subnet's router IP | `x.x.x.1` |
| `<SUBNET_CIDR>` | the server subnet to advertise | `x.x.x.0/24` |
| `<STORAGE>` | storage for the 4G rootfs | `vmstore` |
| `<TEMPLATE>` | Debian standard template | `debian-12-standard_12.12-1_amd64.tar.zst` |

Check the target subnet for a free IP before choosing `<CT_IP>` (ping sweep +
`ip neigh show` from the host; ARP is more trustworthy than ping alone).

## Create the container (on the Proxmox host)

```bash
pveam update
pveam download local <TEMPLATE>

pct create <VMID> local:vztmpl/<TEMPLATE> \
  --hostname <NAME> --unprivileged 1 --ostype debian \
  --cores 1 --memory 512 --swap 512 --features nesting=1 \
  --net0 name=eth0,bridge=<BRIDGE>,firewall=1,gw=<LAN_GW>,ip=<CT_IP><VLAN_OPT> \
  --nameserver "1.1.1.1 9.9.9.9" \
  --rootfs <STORAGE>:4 --onboot 1
```

DNS is pinned to public resolvers on purpose: the gateway must be able to
bootstrap/re-auth even when local DNS (Pi-hole) is down.

## TUN device passthrough

Append to `/etc/pve/lxc/<VMID>.conf` (works in unprivileged CTs):

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

## Inside the container

```bash
pct start <VMID>
pct exec <VMID> -- bash
```

```bash
apt-get update && apt-get install -y curl ca-certificates
curl -fsSL https://tailscale.com/install.sh | sh

printf "net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n" \
  > /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf

tailscale up --advertise-routes=<SUBNET_CIDR> --advertise-exit-node
```

`tailscale up` prints an auth URL — open it as the tailnet owner.

## Admin console (manual, once per gateway)

1. Machines → the new node → **Edit route settings** → approve the subnet
   route and exit node.
2. Node menu → **Disable key expiry** (gateways must survive unattended).

## Verify

From any tailnet machine:

```bash
tailscale status --json | jq -r '.Peer[] | select(.HostName=="<NAME>") | {routes: .PrimaryRoutes, exit: .ExitNodeOption}'
```

Both site gateways must show identical route sets. Note only one is *primary*
for a subnet at a time (`PrimaryRoutes` lists what this node currently serves);
`AllowedIPs` / admin console show what is approved. Then from a remote node,
ping something on the subnet with the other gateway's tailscale stopped if you
want to prove failover.

## Notes / known drift

- 2026-08-27: gw-01 was found with *runtime-only* forwarding — both sysctl
  lines commented out in `/etc/sysctl.conf`, `ip_forward=1` set by hand, IPv6
  forwarding off entirely (admin console showed "Unable to relay traffic").
  A CT reboot would have silently dropped subnet routing. Fixed by writing
  `/etc/sysctl.d/99-tailscale.conf` as above. The sysctl.d file is part of
  this recipe precisely so this can't recur.
- Both gateways log the tailscale "UDP GRO forwarding suboptimal" warning.
  Optional tuning is an `ethtool` change on the *host* physical NIC
  (https://tailscale.com/s/ethtool-config-udp-gro); not applied — revisit if
  exit-node throughput matters.
- Container is restart-safe: `onboot=1`, tailscaled re-auths with its stored
  node key, static IP means no DHCP dependency.
