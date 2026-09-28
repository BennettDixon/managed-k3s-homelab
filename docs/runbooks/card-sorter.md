# card-sorter — runbook

The card-sorting machine's backend on k3s: `inventory` (every tray's manifest, the only record of where ~7,000 cards are) and `identify` (a seat photo → a printing, against a local Scryfall mirror). Specs in the private card-sorter repo, `docs/specs/inventory.md` and `docs/specs/identify.md`; manifests `apps/base/card-sorter/`; the machine's agent runs on the sorter's Pi (a tagged tailnet node) and only ever pulls. Two PRs: inventory first, identify when its own gate holds (the identify files are in the base, commented out of `kustomization.yaml`).

## Operator prerequisites (only a human can do these)

1. **Harbor** (`https://harbor-ui…` on the tailnet as admin, never a password on a command line): private project `card-sorter`; robot `card-sorter-pull` (full name `robot$card-sorter+card-sorter-pull`) scoped to `repository:pull` on it, never expiring — the secret is shown ONCE: straight into `terraform.tfvars`; the docker-push user as maintainer. Both images live in this one project.
2. **NAS** (`truenas-bulk-52tb`, appliance tier), from its shell as root:
   - datasets `BulkPoolZ2/artifacts/card-sorter`, `…/card-sorter/scryfall` (the mirror, ~150 GB English) and `…/card-sorter/litestream` (the replica; a dataset of its own so a mirror prune can never touch it);
   - an NFS export of `/mnt/BulkPoolZ2/artifacts/card-sorter` to the compute network, root mapped to root, like the existing `phyt-minio` export (`midclt call sharing.nfs.create`), and `showmount -e truenas-bulk-52tb` from the **k3s node** showing it — the node must resolve the tailnet name, which is the first thing to check;
   - a user `litestrm` (TrueNAS refused the longer name) with home `/mnt/BulkPoolZ2/artifacts/card-sorter/litestream/litestrm`, the directory the UI appends inside the dataset, shell `sh`, no password, no Samba, and as its only authorized key the public half of a fresh key pair made on the workbench: `ssh-keygen -t ed25519 -f ~/.ssh/nas_litestream -N ''`. The private half goes into `terraform.tfvars` as `card_sorter_litestream_private_key` (a heredoc; the whole file, newlines included); the pair is never used for anything else.
3. **AWS SSO login** — every plan/apply blocks on it.
4. **The Pi on the tailnet** as a tagged node `tag:sorter` with key expiry off, and an ACL grant `tag:sorter → inventory:80, identify:80` (agent spec sign-off 2). The Pi's token (`sorter-01`) goes in `/etc/sorter-agent/token`, mode 600.
5. **The egress path to the NAS**, found at the first deploy (2026-09-28): pods cannot resolve tailnet names, so the sidecar reaches the NAS through the operator's egress Service `nas` (`egress-nas.yaml`), which needs two out-of-band things: the key `NAS_TAILNET_FQDN` in the `cluster-vars` secret in `flux-system` (`kubectl -n flux-system patch secret cluster-vars -p '{"stringData":{"NAS_TAILNET_FQDN":"<the NAS's ts.net FQDN>"}}'`, never committed), and the ACL `tag:k8s -> truenas-bulk-52tb:22` in the admin console, since the proxy is a `tag:k8s` node and the NAS's sshd only sees it as such.
6. **No tailnet node named `inventory` or `identify`** — checked 2026-09-28 (none); re-check at merge with `tailscale status`, because MagicDNS would silently mint `inventory-1`.

## MERGE GATE (PR 1 — inventory) — order is load-bearing

The apps chain uses `wait: true` + `dependsOn`, so ONE unready object (an ExternalSecret that cannot sync, a pod that cannot pull, a readiness that never passes) leaves the `apps` Kustomization NotReady on a ~7 min retry cycle. Do NOT merge until ALL of these exist:

1. **The three AWS SM entries — terraform-managed** (`terraform/main.tf`: `card_sorter_caller_tokens_secret`, `card_sorter_litestream_sftp_secret`, `card_sorter_harbor_docker_pull_secret`): set the six `card_sorter_*` values in `terraform.tfvars` (template: `terraform.tfvars.empty`; tokens with `openssl rand -hex 32`, each different), then a **TARGETED** plan naming the three modules AND the reader policy — full applies stay forbidden while the Lightsail proxy drifts:

   ```bash
   cd terraform && terraform plan \
     -target=module.card_sorter_caller_tokens_secret \
     -target=module.card_sorter_litestream_sftp_secret \
     -target=module.card_sorter_harbor_docker_pull_secret \
     -target=aws_iam_policy.external_secrets_reader
   ```

   Expected: 6 to add (three secrets, three versions), 1 to change (the policy), 0 to destroy. Apply with the same targets.
2. **Harbor project + robot** — prerequisite 1 — and the image pushed at the manifest's tag, from the workbench, with the docker-driver builder (a `docker-container` builder does not trust the homelab root CA):

   ```bash
   cd ~/projects/card-sorter/services/inventory && docker buildx build --builder desktop-linux --platform linux/amd64 -t harbor.internal/card-sorter/inventory:0.1.0 --push .
   ```

3. **The NAS side** — prerequisite 2 — proven from the workbench with a scratch database before anything on the cluster depends on it:

   ```bash
   litestream replicate /tmp/scratch.db "sftp://litestrm@truenas-bulk-52tb:22/mnt/BulkPoolZ2/artifacts/card-sorter/litestream/litestrm/scratch?key-path=$HOME/.ssh/nas_litestream"
   ```

   Write a few rows into `scratch.db` in another shell, stop it, then `litestream restore -o /tmp/restored.db "<the same URL>"` and compare the row counts. If the URL form refuses the key (0.5.17 ignores `key-path` in a URL), use a config file with the same fields as `apps/base/card-sorter/litestream.yml` and `-config`. **This is the rehearsed restore the spec asks for**; record it in the table at the end, then delete `scratch/` on the NAS.
4. **`callers.yaml` parses** and names exactly the ids in the SM map (`sorter-01`, `bennett`; `identify` is listed for PR 2 and authenticates nobody until its token exists) — CI builds the base, and the service refuses to become ready on a bad registry.
5. **No tailnet name collision** — prerequisite 6; the egress path — prerequisite 5.
6. Merge; then the "First run after merge" checks below. One reconciliation-chain change per window.

## MERGE GATE (PR 2 — identify)

Everything above, and, in order:

1. **The NFS export covers the child dataset.** A FreeBSD export of `card-sorter` does not cross into `card-sorter/scryfall`, its own dataset: the first mount answered "access denied by server" (2026-09-28). The share entry lists both paths, `/mnt/BulkPoolZ2/artifacts/card-sorter` and `/mnt/BulkPoolZ2/artifacts/card-sorter/scryfall` (UI: Sharing → NFS → edit → Add path; or `midclt call sharing.nfs.update <id> '{"paths": [...]}'`).
2. **The node mounts it.** The kubelet performs the mount, and the node does not resolve tailnet names (a mount by name never reached the server), so the PersistentVolume's server is `${NAS_LAN_IP}` from `cluster-vars` (added 2026-09-28; the phyt volumes use the same address). Proof: a throwaway pod in `card-sorter` with the same `nfs:` spec that lists the mount and touches a file, deleted afterwards.
3. **The `inventory` caller in the SM map:** `card_sorter_inventory_token` in tfvars (`openssl rand -hex 32`) and a targeted apply of `module.card_sorter_caller_tokens_secret` (the version is replaced: 1 add, 1 destroy, 0 change of the secret itself); ESO force-sync; `kubectl -n card-sorter rollout restart deploy/inventory`, since the map is read at boot.
4. **The full English mirror on the NAS**, rsynced from the workbench when its run ends (`rsync -a ~/scryfall-mirror/ root@truenas-bulk-52tb:/mnt/BulkPoolZ2/artifacts/card-sorter/scryfall/` on `agent-mini`), then `identify verify --sample 500` against it. Merging on a partial mirror is possible — the CronJob continues from the manifest — but the catalogue would lack most printings until the next Sunday.
5. **The image pushed at the manifest's tag**, `identify:0.2.0` (done 2026-09-28: the matcher with Tesseract baked in).
6. **The eval gate** (identify spec §5) is deliberately NOT in this gate: the labelled box and the imaging head do not exist yet. The service merges and serves; the agent's scan jobs stay off until the eval sets the threshold and the calibration, and the matcher's confidence is the uncalibrated table until `/data/calibration.json` exists.

Then merge in one PR. The first boot's init container builds the catalogue from the mirror (minutes on a partial mirror, about an hour on the full one), during which the `apps` Kustomization is NotReady on purpose: schedule it, and merge nothing else in that window.

## First run after merge (verify on the cluster, never on GitHub)

```bash
flux get kustomizations                                  # apps Ready at the merge commit
kubectl -n card-sorter get externalsecrets               # all SecretSynced
kubectl -n card-sorter get pods                          # inventory 2/2
kubectl -n card-sorter logs deploy/inventory -c litestream --tail=20   # "snapshot complete", no sftp errors
curl -s http://inventory/readyz                          # {"ok": true}
```

In Prometheus: `up{namespace="card-sorter"} == 1` for both endpoints, `litestream_db_size{namespace="card-sorter"} > 0`, the `card-sorter.rules` group present. Then, from the workbench with the operator token: `POST /trays` for the four bench trays and print their labels (`GET /trays/labels.pdf`). If an ExternalSecret sits unsynced after a correct apply, annotate it `force-sync=$(date +%s)`.

## Restore the database from the replica

When the PVC is lost, the node is rebuilt, or the file fails its boot conservation check and the last good state is wanted back.

1. Stop the writer: `kubectl -n card-sorter scale deploy/inventory --replicas=0` and wait for the pod to go.
2. From an ephemeral pod on the same PVC with the same key and config:

   ```bash
   kubectl -n card-sorter apply -f - <<'EOF'
   apiVersion: v1
   kind: Pod
   metadata: {name: inventory-restore}
   spec:
     restartPolicy: Never
     securityContext: {runAsUser: 10001, fsGroup: 10001}
     containers:
     - name: litestream
       image: litestream/litestream:0.5.17
       command: ["sh", "-c", "sleep 3600"]
       volumeMounts:
       - {name: data, mountPath: /data}
       - {name: config, mountPath: /etc/litestream.yml, subPath: litestream.yml}
       - {name: sshkey, mountPath: /ssh, readOnly: true}
     volumes:
     - {name: data, persistentVolumeClaim: {claimName: inventory-data}}
     - {name: config, configMap: {name: inventory-litestream}}
     - {name: sshkey, secret: {secretName: card-sorter-litestream, defaultMode: 0400}}
   EOF
   kubectl -n card-sorter wait pod/inventory-restore --for=condition=Ready --timeout=120s
   kubectl -n card-sorter exec inventory-restore -- sh -c 'mv /data/inventory.db /data/inventory.db.broken.$(date +%s) 2>/dev/null; rm -f /data/inventory.db-wal /data/inventory.db-shm'
   kubectl -n card-sorter exec inventory-restore -- litestream restore -config /etc/litestream.yml -o /data/inventory.db /data/inventory.db
   kubectl -n card-sorter delete pod inventory-restore
   ```

   The ConfigMap's name carries a kustomize hash: read it from `kubectl -n card-sorter get cm`. For a point in time add `-timestamp 2026-10-01T12:00:00Z`; the replica keeps 30 days.
3. `kubectl -n card-sorter scale deploy/inventory --replicas=1` and watch `readyz`: the conservation check at boot says whether the restored file is sound.
4. The agent's journal on the Pi replays any step the replica missed (about a second of moves): it gets `idempotent_replay` or a mismatch it reconciles (agent spec §4). Nothing to do by hand unless a job shows `blocked`.
5. The seat crops are not in the database; they are rsynced nightly to the same NAS dataset. A missing crop is a card a person cannot fix from a picture, not a lost card.

## Rotation

A caller token: tfvars → targeted apply on `module.card_sorter_caller_tokens_secret` → ESO refresh (or `force-sync`) → `kubectl -n card-sorter rollout restart deploy/inventory` (the map is read at boot) → the new value into `/etc/sorter-agent/token` on the Pi → `sudo systemctl restart sorter-agent`. The Litestream key: a new pair, the public half replaces the NAS user `litestrm`'s authorized key, the private half through tfvars and the same targeted apply, then the pod restart. The Harbor robot: as the gateway's.

## Alerts — what pages vs what waits for morning

| Alert | Pages | Meaning, and what to do |
| --- | --- | --- |
| `SorterNeedsHuman` | yes | The machine has waited 10 min for a person: the panel on the Pi says what (a tray swap, a jam). Do it, press resume |
| `InventoryReplicaDown` | yes | The Litestream sidecar is gone: the PVC is the only copy. `kubectl logs -c litestream`; the key, the NAS, the dataset |
| `InventoryReplicaStale` | morning | Applies happen but no segment shipped for 10 min: the SFTP path |
| `SorterAgentSilent` | morning | A job runs and the Pi has said nothing for 5 min: offline and journaling, or stuck |
| `InventoryInflightStale` | morning | A card left a tray and its job never said where: queue a rescan of the untrusted tray |
| `InventoryImagesLarge` | morning | The crop store is near 2 GiB: prune, a human task |
| `InventoryDbOversized` | morning | The file is far bigger than 7,000 cards can explain |
| `InventoryMetricsAbsent` | morning | The scrape target is gone; every rule above is blind |

## Rehearsal record

| Date | What | Result |
| --- | --- | --- |
| 2026-09-28 | `litestream replicate` (0.5.17) of a scratch database to a **file** replica on the workbench, rows written, `litestream restore` to a second file, row counts compared | 4 = 4; the SFTP form waits for the NAS user (prerequisite 2) |
| 2026-09-28 | The same over **SFTP** to the NAS user `litestrm`, with the config-file form the sidecar uses (`key-path`, the replica path under the user's home), rows written while replicating, restored from the NAS | 6 = 6; 4 files, 181 KB on the NAS; scratch removed afterwards. The URL form of `litestream replicate` ignores `key-path`, so rehearse with a config file |
