#!/usr/bin/env bash
#
# One-time distribution of the homelab root CA to clients that pin Harbor's
# TLS.
#
# Harbor's leaf certificate still rotates (cert-manager renews it every ~60
# days), but it is signed by the long-lived homelab-root-ca (see
# infrastructure/configs/cluster-issuers.yaml). Clients must trust the ROOT,
# not the leaf, so rotations stop breaking them. Run this once per client,
# and again only if the root itself is ever re-issued (~2033):
#
#   - k3s nodes: containerd trusts Harbor via the ca_file referenced in
#     /etc/rancher/k3s/registries.yaml (installed over ssh)
#   - this machine: docker trusts Harbor via ~/.docker/certs.d/<host>/ca.crt
#
# Usage:
#   ./scripts/distribute-harbor-ca.sh [node ...]
#
#   node: ssh targets of k3s nodes (default: "k3s"). ssh/sudo prompt for
#         passwords as usual.
#
# Env overrides:
#   REGISTRY_HOST  registry hostname            (default: harbor.internal)
#   SSH_USER       ssh user on the nodes        (default: k3s)
#   CA_SECRET_NS   namespace of the CA secret   (default: cert-manager)
#   CA_SECRET_NAME name of the CA secret        (default: homelab-root-ca)

set -euo pipefail

REGISTRY_HOST="${REGISTRY_HOST:-harbor.internal}"
SSH_USER="${SSH_USER:-k3s}"
CA_SECRET_NS="${CA_SECRET_NS:-cert-manager}"
CA_SECRET_NAME="${CA_SECRET_NAME:-homelab-root-ca}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
node_installer="$script_dir/install-node-harbor-ca.sh"

NODES=("$@")
if [ ${#NODES[@]} -eq 0 ]; then
  NODES=(k3s)
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
ca_file="$workdir/homelab-root-ca.crt"

echo "==> Fetching root CA from secret $CA_SECRET_NS/$CA_SECRET_NAME"
kubectl get secret -n "$CA_SECRET_NS" "$CA_SECRET_NAME" \
  -o jsonpath='{.data.ca\.crt}' | base64 -d >"$ca_file"
openssl x509 -in "$ca_file" -noout -subject -enddate

echo "==> Checking $REGISTRY_HOST serves a chain rooted in this CA"
if echo | openssl s_client -connect "$REGISTRY_HOST:443" \
  -servername "$REGISTRY_HOST" -CAfile "$ca_file" 2>/dev/null |
  grep -q 'Verify return code: 0 (ok)'; then
  echo "    OK: served certificate validates against the root CA"
else
  echo "    WARNING: $REGISTRY_HOST is not (yet) serving a cert signed by this root." >&2
  echo "    If the homelab-ca switch just deployed, give cert-manager/ingress a" >&2
  echo "    minute and re-run. Distribution continues anyway." >&2
fi

for node in "${NODES[@]}"; do
  echo "==> Installing root CA on node: $node"
  scp "$ca_file" "$node_installer" "$SSH_USER@$node:/tmp/"
  ssh -t "$SSH_USER@$node" "sudo sh /tmp/install-node-harbor-ca.sh && rm -f /tmp/install-node-harbor-ca.sh /tmp/homelab-root-ca.crt"
done

echo "==> Installing root CA for docker on this machine"
certs_dir="$HOME/.docker/certs.d/$REGISTRY_HOST"
mkdir -p "$certs_dir"
cp "$ca_file" "$certs_dir/ca.crt"
echo "    wrote $certs_dir/ca.crt"
echo "    Docker Desktop only reads certs.d at startup: restart it, then"
echo "    'docker login $REGISTRY_HOST' / push as usual."

echo "==> Done. Leaf rotations now chain to the distributed root; no client"
echo "    changes are needed until the root itself renews."
