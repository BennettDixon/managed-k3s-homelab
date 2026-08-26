# Harbor
[harbor](https://github.com/goharbor/harbor) is a free and open source private container registry. Harbor is a CNCF graduated project that allows you to setup a private artifact registry with features like vulnerability scanning, RBAC, SSO, and more.

This is my installation of harbor for my homelab, allowing me to host my own private artifact registry and avoid the expenses of a cloud based registry. It was also a very fun experience to setup my own registry at home.

## TLS trust model

`harbor.internal` can't get a public (Let's Encrypt) certificate, so its TLS
chains to a private root CA managed by cert-manager:

- `homelab-root-ca` (Certificate in `infrastructure/configs/cluster-issuers.yaml`):
  a 10-year self-signed root, stored in the `homelab-root-ca` secret in the
  `cert-manager` namespace.
- `homelab-ca` (ClusterIssuer): signs leaf certificates with that root.
- Harbor's ingress requests its certificate from `homelab-ca` via the
  `cert-manager.io/cluster-issuer` annotation in `apps/*/harbor-values.yaml`.

cert-manager still rotates the *leaf* every ~60 days, but because it chains to
the stable root, clients that trust the root keep working across rotations.
(Previously the ingress used the bare `selfsigned` issuer, so every rotation
minted a brand-new self-signed leaf and broke every client that had pinned the
old one: containerd on the k3s nodes -> `ImagePullBackOff`, and docker on the
Mac -> failed pushes.)

### One-time client trust setup

Run (from this repo, with kubectl pointed at the cluster):

```bash
./scripts/distribute-harbor-ca.sh [node ...]
```

It extracts the root CA from the cluster and installs it:

- on each k3s node, at every `ca_file` path referenced by
  `/etc/rancher/k3s/registries.yaml`. Refreshing the *contents* of an
  already-referenced ca_file needs no restart (containerd re-reads it per
  pull), but adding or changing the `ca_file` entry itself requires one
  `sudo systemctl restart k3s` to regenerate containerd's registry config.
  (Prior to 2026-08-26 the node side-stepped leaf rotations with
  `insecure_skip_verify: true`; that has been replaced with `ca_file`
  verification against the root.)
- on this machine, at `~/.docker/certs.d/harbor.internal/ca.crt`
  (restart Docker Desktop afterwards)

Manual equivalent:

```bash
kubectl get secret -n cert-manager homelab-root-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > homelab-root-ca.crt
```

then place that file at the node's `ca_file` path and in
`~/.docker/certs.d/harbor.internal/ca.crt`.

### Verifying

```bash
openssl s_client -connect harbor.internal:443 -servername harbor.internal -CAfile homelab-root-ca.crt </dev/null | grep 'Verify return code'
# want: Verify return code: 0 (ok)
```

### When the root renews

The root lasts 10 years; cert-manager re-issues it at ~2/3 of that (~2033).
When that happens (or if the `homelab-root-ca` secret is ever deleted and
recreated), re-run the distribution script once.