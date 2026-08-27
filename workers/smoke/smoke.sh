#!/usr/bin/env bash
# Subscription-lane smoke test for worker nodes.
# Runs a trivial `claude -p` task headlessly and ships the JSON result plus a
# timestamped log to the bulk NAS over the tailnet. Proves the whole chain:
# worker -> Claude (subscription lane) -> artifact on storage.
#
# Deployed at: /opt/worker/smoke.sh on worker-01 (canonical copy lives in the
# repo at workers/smoke/smoke.sh — edit there, redeploy with pct push).
# Requires: /etc/worker/claude.env containing CLAUDE_CODE_OAUTH_TOKEN=...
#           (created by `claude setup-token`, root-only, never committed).
set -euo pipefail

ENV_FILE=/etc/worker/claude.env
NAS=truenas-bulk-52tb
NAS_DIR=/mnt/BulkPoolZ2/artifacts/worker-smoke

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$STAGE/smoke-$TS.log"
OUT="$STAGE/result-$TS.json"
touch "$OUT"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }

[ -f "$ENV_FILE" ] || { echo "FATAL: missing $ENV_FILE" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${CLAUDE_CODE_OAUTH_TOKEN:?not set in $ENV_FILE}"

log "smoke start host=$(hostname) claude=$(claude --version 2>/dev/null || echo unknown)"

STATUS=ok
if ! (cd "$STAGE" && timeout 300 claude -p \
    'Reply with a single JSON object exactly like {"smoke":"ok","model":"<your model id>"}' \
    --output-format json >"$OUT" 2>>"$LOG"); then
  STATUS=failed
fi
log "claude -p status=$STATUS result_head=$(head -c 200 "$OUT" | tr '\n' ' ')"

# Ship even on failure — a failed run landing on storage is still signal.
DEST="$NAS_DIR/$TS"
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$NAS" "mkdir -p '$DEST'"
scp -q -o BatchMode=yes "$OUT" "$LOG" "root@$NAS:$DEST/"
log "shipped to $NAS:$DEST"

[ "$STATUS" = ok ]
