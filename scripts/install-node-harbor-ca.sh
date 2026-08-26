#!/bin/sh
#
# Runs ON a k3s node as root (scripts/distribute-harbor-ca.sh copies it over
# together with /tmp/homelab-root-ca.crt and invokes it via sudo).
#
# Installs the homelab root CA at every ca_file path referenced in
# /etc/rancher/k3s/registries.yaml so containerd validates Harbor's TLS
# against the stable root instead of a pinned (rotating) leaf. containerd
# re-reads the ca_file on each pull, so no k3s restart is needed.

set -eu

REGISTRIES=/etc/rancher/k3s/registries.yaml
SRC=/tmp/homelab-root-ca.crt

if [ ! -f "$SRC" ]; then
  echo "error: $SRC not found (scp it over first)" >&2
  exit 1
fi

paths=""
if [ -f "$REGISTRIES" ]; then
  # Extract ca_file values, quoted or not, e.g.:  ca_file: "/etc/rancher/k3s/harbor.crt"
  paths=$(sed -n 's/.*ca_file:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*/\1/p' "$REGISTRIES" | sort -u)
fi

if [ -z "$paths" ]; then
  fallback=/etc/rancher/k3s/homelab-root-ca.crt
  echo "warning: no ca_file entries found in $REGISTRIES" >&2
  echo "Installing to $fallback -- reference it there, e.g.:" >&2
  echo '  configs:' >&2
  echo '    "harbor.internal":' >&2
  echo '      tls:' >&2
  echo "        ca_file: $fallback" >&2
  echo "then restart k3s to regenerate containerd config." >&2
  paths=$fallback
fi

for p in $paths; do
  install -m 0644 "$SRC" "$p"
  echo "installed root CA -> $p"
done
