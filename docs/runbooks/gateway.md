# Runbook: gateway

Operator-facing steps only — semantics (interface, lanes, the budget model,
the ledger, alerts) live in the spec: `docs/specs/gateway.md`. Manifests:
`apps/base/gateway/`. Source: `services/gateway/`. Endpoint:
`http://gateway/v1/…` (tailnet-only; one caller token per caller id, used as
the client's `OPENAI_API_KEY`). Terraform: three modules in
`terraform/main.tf`, their ARNs in `terraform/iam-external-secrets.tf`.

**Status: slice 2 opened 2026-09-10 — NOT merged, nothing on the cluster.**
v1 is metered-only (spec SIGN-OFF 9); the subscription lane is deferred.

## Operator prerequisites (only a human can do these)

Each is a gate step below; none can be scripted from an agent session.

1. **Console workspace `homelab-gateway`** (Anthropic Console → Settings →
   Workspaces): create it, set its **monthly spend limit to $1 FIRST** (the
   deliberate trip, "First live call" step 4), then mint a **workspace-scoped
   API key** in it and write the value straight into `terraform.tfvars` as
   `gateway_anthropic_api_key` — never into a shell command, never into a
   chat. Spec §7.3: the limit must exist before the key does; after the trip
   it is raised to the attested `console_workspace_limit_usd` in
   `apps/base/gateway/registry.yaml` ($50, ≈ 2 × Σ project caps). If the
   Console refuses a $1 limit, use the smallest it accepts and scale the
   trip. No Admin API key is provisioned in v1.
2. **AWS SSO login** (the browser step) — every plan/apply blocks on it.
3. **Harbor** (`https://harbor-ui…` on the tailnet as `bennett`/`admin`, or
   the API with a prompted password — never on a command line): private
   project `gateway`; robot `gateway-pull` (full name
   `robot$gateway+gateway-pull`) scoped to `repository:pull` on it, never
   expiring — the secret is shown ONCE: straight into `terraform.tfvars`;
   the docker-push user as maintainer (mirror of `knowledge`). Then push
   the image at the manifest's tag (gate step 4).
4. **No tailnet node named `gateway`** — checked 2026-09-10 (none); re-check
   at merge (`tailscale status`), because MagicDNS would silently mint
   `gateway-1` and `http://gateway` would resolve to nothing.

## MERGE GATE (slice 2 — manifests) — order is load-bearing

The apps chain uses `wait: true` + `dependsOn`, so ONE unready gateway
object (an ExternalSecret that cannot sync — including an SM entry the ESO
reader is not allowed to read — a pod that cannot pull its image, a registry
that fails readiness) leaves the `apps` Kustomization NotReady on a ~7 min
retry cycle for as long as the gate is missed. One reconciliation-chain
change per window. Do NOT merge the manifests PR until ALL of these exist:

1. **ESO identity is the scoped reader** — done 2026-09-09
   (`docs/runbooks/external-secrets-iam.md`). The three gateway ARNs are in
   its policy in the same PR as the modules (below); this is what makes a
   dollar-spending key acceptable on the secret path (spec §7.2).
2. **Console workspace + spend limit + key** — prerequisite 1.
3. **The three AWS SM entries — terraform-managed** (`terraform/main.tf`):
   set the five `gateway_*` values in `terraform.tfvars` (template:
   `terraform.tfvars.empty`), then a **TARGETED** plan naming the modules
   AND the policy — full applies stay forbidden while the Lightsail proxy
   drift is parked (STATUS):
   ```bash
   aws sts get-caller-identity   # a live SSO session, or STOP here
   cd terraform && terraform plan \
     -target=module.gateway_caller_tokens_secret \
     -target=module.gateway_anthropic_api_key_secret \
     -target=module.gateway_harbor_docker_pull_secret \
     -target=aws_iam_policy.external_secrets_reader
   ```
   Read the plan: **6 to add, 1 to change, 0 to destroy** — two resources
   per secret module, and the ESO reader policy document changes IN PLACE
   (every earlier runbook's "0 to change" habit breaks here; that one
   change is expected). Anything to destroy (especially the Lightsail
   public proxy) means the targets were dropped — STOP, do not apply. Then
   apply the same four targets. Value sources:
   - `gateway_operator_token`, `gateway_n8n_executor_token` — generated
     (`openssl rand -hex 32` each), ≥ 16 chars, must differ (the pod refuses
     duplicate values at boot). Every day-one token is minted here: one SSO
     login for the whole build. The n8n-executor value ALSO goes into the
     n8n LXC env as `GATEWAY_EXECUTOR_TOKEN` at slice 3, not before.
   - `gateway_anthropic_api_key` — prerequisite 1.
   - `gateway_harbor_docker_pull_username` / `_password` — the robot from
     prerequisite 3 (full `robot$gateway+gateway-pull` name).
4. **Harbor project + robot + the image PUSHED** at the manifest's tag:
   ```bash
   cd services/gateway
   docker buildx build --platform linux/amd64 \
     -t harbor.internal/gateway/gateway:0.1.0 --push .
   ```
   The tag must equal the one in `apps/base/gateway/deployment.yaml`
   (0.1.0; the version-bump CI guard keeps it equal to `pyproject.toml`).
   Use the `desktop-linux` docker-driver builder (the default context on the
   workbench; `--builder default` is a context name there and fails):
   `docker-container` builders push from inside BuildKit, which does not
   trust the homelab root CA. The Dockerfile's base-image tags
   (`python:3.12-slim`, `ghcr.io/astral-sh/uv:0.10.10`) were proven to build
   on 2026-09-10 (54 MB image, user 10001, boots read-only, 73 MiB RSS).
5. **Registry parseable**: `apps/base/gateway/registry.yaml` passes the
   service's admission rules AND Σ `cap_usd` ≤ `console_workspace_limit_usd`.
   CI runs `tests/test_registry_manifest.py` against the real file (and the
   deployment's env) on every PR touching either; locally
   `uv run pytest -q tests/test_registry_manifest.py` in `services/gateway`.
6. **No tailnet node named `gateway`** — prerequisite 4.
7. **Merge, watch Flux** ("First run after merge", below).

### Secret-onboarding checklist (spec §7.5 — the house convention)

| # | item | this PR |
|---|---|---|
| 1 | module in `terraform/main.tf`, `jsonencode` shape, description naming every out-of-band holder | ✅ three modules |
| 2 | `variables.tf` + `terraform.tfvars.empty` | ✅ five vars |
| 3 | `.secret_arn` in `iam-external-secrets.tf`, same PR | ✅ three ARNs |
| 4 | ExternalSecret in the app base (prod-only), never `apps/secrets/` | ✅ three in `apps/base/gateway/` |
| 5 | `aws sts get-caller-identity` before planning | operator, gate step 3 |
| 6 | targeted plan naming the modules AND the policy — 6 add / 1 change / 0 destroy | operator, gate step 3 |
| 7 | apply; after merge `kubectl get externalsecrets -A` all `SecretSynced` | operator, gate step 3 + first run |
| 8 | update the out-of-band holders | workbench env now; n8n env at slice 3 |
| 9 | the runbook's rotation section names every holder | ✅ "Rotation", below |
| 10 | a STATUS decisions-log line | after merge, with the live-call results |

## Deploy / rollback

Image is built and pushed from the workbench; deploys are image-tag bumps in
`apps/base/gateway/deployment.yaml` (CI refuses an image-input change
without the version + tag bump: "merged ≠ landed" applies doubly to a
service that spends); registry (`registry.yaml`) changes hash-roll the
Deployment (Recreate: seconds of downtime, never two writers). Rollback =
revert the tag commit — migrations are additive-only, the DB reopens. Every
restart runs the boot sweep first: any request in flight at the moment of
the old pod's SIGTERM settles AT its reservation (over-count, never under),
shows as `E_SWEPT` rows and fires `GatewaySweptSpend` — expected on every
deploy that interrupted a call; reconcile at the monthly line.

## First run after merge (verify on the cluster, never on GitHub)

1. **Flux**: `kubectl -n flux-system get kustomization apps` Ready at the
   merge commit; `kubectl -n gateway get externalsecret` → all three
   `SecretSynced`; `kubectl -n gateway get pods` → `1/1 Running` with
   `kubectl -n gateway get deploy gateway -o jsonpath='{.spec.template.spec.containers[0].image}'`
   = `harbor.internal/gateway/gateway:0.1.0`. Diagnose from kubectl, not
   the tailnet — a NotReady pod has no Service endpoints, so
   `http://gateway` simply refuses connections: `SecretSyncedError` with
   `AccessDenied` = gate step 1/3 (the ARN missing from the reader policy,
   or the apply not run); `ImagePullBackOff` = gate 4 (image not pushed) OR
   the pull-robot entry missing; `CreateContainerConfigError` = a token map
   or key Secret missing; `0/1 Running` + `kubectl -n gateway logs
   deploy/gateway | grep registry_parse_failed` = gate 5; `/readyz` 503
   with a `reason` = read it (write ping, clock guard, quick_check,
   scope_totals mismatch — all in the body). After fixing a gate miss
   post-merge, ESO's error backoff can take ~17 min to retry — annotate the
   ExternalSecret with `force-sync=$(date +%s)` to shortcut it.
2. **Probes over the tailnet** (once Ready): `curl http://gateway/healthz`
   and `/readyz` → `{"ok":true,"version":"0.1.0"}`.
3. **Rules and scrape**: `kubectl -n gateway get prometheusrule` present;
   in Prometheus `up{namespace="gateway"}` = 1, `gateway_db_bytes` > 0,
   `gateway_lane_up{lane="metered"}` = 1 (the idle `models.list()` probe
   passed: the key works, the path is open), and the 12 `Gateway*` rules
   loaded (the prometheus container has no curl; query through the
   alertmanager pod's busybox `wget` as `docs/runbooks/alerts.md` shows).
4. **§13 check 2 — `count_tokens` latency from the compute site** (needs the
   key, so it runs here, inside the pod, never from a worker): 20 calls,
   p50/p95; > 500 ms p95 reopens heuristic-first (spec §5):
   ```bash
   kubectl -n gateway exec deploy/gateway -- python - <<'PY'
   import os, time, anthropic
   c = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0)
   t = []
   for _ in range(20):
       s = time.perf_counter()
       c.messages.count_tokens(model="claude-haiku-4-5", messages=[{"role": "user", "content": "Say hi"}])
       t.append((time.perf_counter() - s) * 1000)
   t.sort(); print(f"p50={t[9]:.0f}ms p95={t[18]:.0f}ms max={t[-1]:.0f}ms")
   PY
   ```
   Record the numbers in the "(verify)" table below.

## First live call (from the workbench, operator token)

With the operator token exported as `GATEWAY_TOKEN` (never on the command
line of a shared shell history; `-D -` prints the response headers):

```bash
gw() { curl -sS -D - -X POST http://gateway/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -H "X-Gateway-Project: $1" -H "X-Gateway-Budget-Cap-USD: $2" -d "$3"; echo; }
```

1. **The proof call** — `haiku`, `max_tokens: 5`, cap `0.01` (≈ $0.0002):
   ```bash
   gw gateway-smoke 0.01 '{"model":"haiku","max_tokens":5,"messages":[{"role":"user","content":"Say hi"}]}'
   ```
   Expect `200`, headers `X-Gateway-Lane-Used: metered`, a non-zero
   `X-Gateway-Billed-USD`, `X-Gateway-Request-Id`, and the same values in
   the body's `gateway` object; then the row:
   ```bash
   curl -sS "http://gateway/ledger/requests?project=gateway-smoke&since=0" -H "Authorization: Bearer $GATEWAY_TOKEN"
   ```
   (`since` is unix milliseconds) → one `settled` row with `input_tokens`,
   `output_tokens`, `provider_request_id`, `model_used`, `inference_geo`.
   Then in Prometheus `gateway_billed_usd_total{project="gateway-smoke"}`
   > 0 within a scrape interval. Read the response and the row for the
   "(verify)" table: `model_used`, `inference_geo`, and the pod log line
   `evt=settle` (the provider accepted `service_tier: "standard_only"` and
   the `output_config` names, or the call would have been a 400
   `E_UPSTREAM_ERROR` naming the field).
2. **Cap 0 ⇒ 402**: the same body with cap `0` →
   `402 E_BUDGET_EXCEEDED scope=request`, `x-should-retry: false`, no row,
   no provider call (`gateway_refusals_total{scope="request"}` moved;
   `gateway_requests_total` did not).
3. **Structured output** (spec §13 check 7 / the `count_tokens` grammar
   question): repeat step 1 with `"response_format":{"type":"json_schema",
   "json_schema":{"name":"hi","schema":{"type":"object","properties":{"greeting":{"type":"string"}},"required":["greeting"],"additionalProperties":false}}}`
   and `max_tokens: 64`. Compare the row's `input_tokens` with a plain call
   of the same prompt: a difference is the grammar's token cost, and
   whether `count_tokens` (the reservation) included it shows as
   `settled ≤ reserved` holding or `GatewaySettleOverReserve` firing.
4. **The deliberate $1 workspace-limit trip** (spec §7.3, the cheapest
   proof that a leaked key's blast radius is the limit, not the org).
   Precondition: the workspace limit is still $1 (prerequisite 1). Spend
   past it on `homelab-ops` with the operator token — Opus at its 16000
   `max_tokens` bills ≈ $0.40 per long answer, so three to five calls:
   ```bash
   for i in 1 2 3 4 5; do gw homelab-ops 5.00 '{"model":"opus","max_tokens":16000,"messages":[{"role":"user","content":"Write a 10,000-word essay on the history of the electrical grid."}]}' | head -20; done
   ```
   The provider's limit enforcement lags its usage aggregation, so keep
   calling until a call answers **`503 E_LANE_UNAVAILABLE`** with a
   `Retry-After` — bounded in the worst case by the $5 daily brake
   (`402 scope=brake_metered`, which would itself page
   `GatewayMeteredBrakeTripped`: fine, that is the other half of the proof;
   reset it afterwards). Record the provider's exact error shape from the
   pod log (`evt=lane_down`, with `status`, `error_type`, whether a
   `retry-after` header was present) in the "(verify)" table. Then:
   `gateway_lane_up{lane="metered"}` = 0, `/ledger/lanes` shows the lane
   down with `cooling_until`, and after 30 min **`GatewayLaneDown`**
   reaches Telegram (`GatewaySpendRateAnomaly` may already have fired at
   > $2/h — expected only here). The trip costs about $1–2 of real money.
5. **Raise the limit** in the Console to the attested $50
   (`console_workspace_limit_usd`). The lane re-probes hourly while down;
   to shortcut, `kubectl -n gateway delete pod -l app=gateway` (Recreate;
   the sweep finds nothing in flight). Confirm `gateway_lane_up` = 1 and a
   repeat of step 1 succeeds.
6. **STATUS.md** (a decisions-log line + the session log) and
   `mini/mcp-config.md` (the `GATEWAY_TOKEN` line for OpenAI-speaking
   tools: `OPENAI_BASE_URL=http://gateway/v1`,
   `OPENAI_API_KEY=$GATEWAY_TOKEN`, plus `X-Gateway-Project` and
   `X-Gateway-Budget-Cap-USD` as default headers).

## Rotation — one credential per principal per window

**Caller tokens** (`k3s_gateway_caller_tokens`): edit the value in
`terraform.tfvars` → targeted apply of `module.gateway_caller_tokens_secret`
→ the ExternalSecret refreshes within 1 h (or annotate it with
`force-sync=$(date +%s)`) → the pod reads the map at BOOT only:
`kubectl -n gateway delete pod -l app=gateway` (Recreate, seconds; preferred
over `rollout restart`, whose annotation Flux's SSA later strips for a
second bounce) → update every holder of the old value:

| caller id | holder | update |
|---|---|---|
| `operator` | the operator's workbench `~/.zshenv` (`GATEWAY_TOKEN`); any OpenAI-speaking tool configured with it | edit, restart the session |
| `n8n-executor` | the n8n LXC `/etc/n8n/n8n.env` as `GATEWAY_EXECUTOR_TOKEN` (from slice 3) | edit, `systemctl restart n8n` (the alert receiver blinks ~10 s) |
| (deferred) `worker-01` lane token | `/etc/worker/gateway.env` as `GATEWAY_LANE_TOKEN` — only when the subscription lane ships | edit, restart the unit |

A stale holder fails loudly: `401 E_UNAUTHORIZED` at the caller (a jobs-mcp
job's `error.message` from slice 3). Adding a caller = a new map key
(terraform) + a `callers:` line in `registry.yaml` — the PR is where a human
reads what they grant; a token whose id has no registry entry authenticates
nobody. The `agent` workbench account gets its own caller id when it first
needs one, never the operator token.

**Metered key** (`k3s_gateway_anthropic_api_key`, spec §7.4): mint a NEW key
in the same `homelab-gateway` workspace → `terraform.tfvars` → targeted apply
of `module.gateway_anthropic_api_key_secret` → ESO `force-sync` → pod delete
→ a proof call (First live call, step 1; from slice 3 a `gateway-smoke` job)
succeeds → **delete the old key in the Console the same day** (the one
unavoidable overlap, stated). Holders, exhaustively: the SM entry, the pod.
Nobody else, ever — never a worker, never n8n, never CI. A revoked or
skewed key shows as `GatewayLaneAuthFailed` within 10 min; the idle probe
sees it without spending.

**Harbor robot** (`k3s_harbor_docker_pull_gateway`): create a new robot in
the UI, put its name/secret into `terraform.tfvars` → targeted apply of
`module.gateway_harbor_docker_pull_secret` → ExternalSecret refresh → delete
the old robot. Pulls only happen at pod (re)creation, so a stale secret
shows up as `ImagePullBackOff` on the next deploy, never mid-run.

**Console workspace limit**: raising it is a Console change AND a registry
PR moving `console_workspace_limit_usd` (CI enforces Σ caps ≤ it) — the
attestation and the reality move together.

## Diagnosis from a phone (`/ledger/*`, never `kubectl exec`)

Every ledger read is an operator-class GET over the tailnet — from a phone
on the tailnet, a browser with the token pasted into an HTTP client works:

| question | call |
|---|---|
| what did project X spend this month, and what is left | `GET /ledger/projects/homelab-ops` |
| what did job J spend (what an executor reports as `spent_usd`) | `GET /ledger/jobs/<job_id>` (`?caller=n8n-executor` to read another caller's job) |
| which requests ran, and what each cost | `GET /ledger/requests?project=<id>&since=<unix ms>` — metadata rows only: caller, class, project, job, lane, model, state, both money columns, tokens, status, provider request id, latency; never text |
| is the lane up, cooling, auth-failed; is a brake latched | `GET /ledger/lanes` |
| release a tripped brake (audited) | `POST /ledger/brake-reset {"lane":"metered","reason":"…"}` — only after the offender is stopped |

`kubectl` is for the pod's own state (image tag, `/readyz` reason, the
`evt=` log lines); it is never needed to answer a money question.

## Brake tripped (`GatewayMeteredBrakeTripped`) checklist

1. Money has already stopped — nothing is spending. Do not raise
   `GATEWAY_METERED_DAILY_CEILING_USD` to make the page stop.
2. `GET /ledger/requests?project=<each>&since=<start of the UTC day>`:
   find the caller / project / job whose rows sum to the ceiling.
3. If it is a job: `cancel(id)` at jobs-mcp (queued attempts) and, from
   slice 3, read the executor's report; if it is the operator token: find
   the session or tool that looped.
4. Only then `POST /ledger/brake-reset {"lane":"metered","reason":"<what
   looped and what stopped it>"}` — the row lands in `brakes` with the
   caller id. Otherwise the brake releases itself at 00:00 UTC.
5. If the loop was a caller's, consider lowering that caller's
   `max_request_cap_usd` / `max_day_billed_usd` in `registry.yaml` (a PR).

## Alerts — what pages vs what waits for morning

One Telegram route today (`severity` is a label until the notifications
lane splits routes), so "pages" means "worth reading at 3 am":

| alert | sev | pages / morning | first move |
|---|---|---|---|
| `GatewayMeteredBrakeTripped` | critical | **pages** — money stopped, something looped | the checklist above |
| `GatewaySpendRateAnomaly` (> $2 billed / h) | critical | **pages** — the half-brake early warning | `/ledger/requests` for the hour; stop the caller before the brake does |
| `GatewayMetricsAbsent` | warning | morning — but while it fires the two pages are blind | Deployment / Service label / monitor label |
| `GatewayProjectNearCap` | warning | morning | raise `cap_usd` (registry PR) or let it stop at 402 |
| `GatewayFallbackSpend` | warning | morning; in metered-only v1 it is a bug | rows with `fallback=1` |
| `GatewaySweptSpend` | warning | morning | expected after any unclean stop / deploy mid-call; reconcile |
| `GatewaySettleOverReserve` | warning | morning | the reservation margin; `BILLED_PRICE_MULTIPLIER_PCT=110` if US-pinned |
| `GatewayLaneDown` (30 m) | warning | morning | `/ledger/lanes`; the workspace limit, the network, provider 5xx |
| `GatewayLaneAuthFailed` (10 m) | warning | morning | key revoked / rotation skew — "Rotation" |
| `GatewayReservationStuck` | warning | morning | delete the pod; the sweep settles it |
| `GatewayDbOversized` | warning | morning | schedule the pruning task (destructive, human) |
| `GatewayPriceTableStale` (90 d) | warning | morning | registry PR: prices + `prices_as_of` |

Not shipped: `GatewaySubscriptionCooling` (spec §9) — the pod emits no
subscription-lane series in v1; the rule ships with the deferred lane's own
PR so no rule ever describes a lane the running pod lacks (the jobs-mcp #16
lesson). The alert path shares compute-site fate with the gateway: a
site-down event delivers nothing and spends nothing.

## Monthly reconcile — Σ billed vs the Console

On the first of the month (UTC), for the previous month:

1. Ledger side: `GET /ledger/projects/<id>` for each project (or sum
   `gateway_billed_usd_total` over the month in Prometheus — counters
   survive restarts within a scrape, but the ledger is authoritative).
   Include `E_SWEPT` / `timeout` / `aborted` rows: they were charged at the
   reservation (an over-count by design).
2. Console side: the `homelab-gateway` workspace's usage/cost for the month.
3. **> 10 % apart reopens the price table** (spec §4): check `models:`
   against the current published rates, update them and `prices_as_of` in
   a registry PR; a persistent gap with correct prices means a reservation
   settled blind (swept rows) or `inference_geo` pricing (the 1.1×
   multiplier). Automatic reconciliation through the Admin API is a §11
   non-feature until the delta exceeds 10 % twice.
4. One line in STATUS.

## Slice-2 `(verify)` items — measured answers

Filled at the first live call; blank = not yet measured. Spec §5, §6.1,
§13 items 1, 2, 7, 10, 14, 15.

| item | expected / hypothesis | measured (date) |
|---|---|---|
| §13-1 pod → `https://api.anthropic.com/v1/models` | 401 without a key proves the path | **✅ 2026-09-10**: 401 ×3 from a scratch pod, ~110 ms each |
| §13-2 `count_tokens` p50 / p95 from the compute site | < 500 ms p95, else heuristic-first | — (First run step 4) |
| §13-10 no tailnet node named `gateway` | none | **✅ 2026-09-10** (re-check at merge) |
| §13-14 no literal `${…}` in any gateway manifest / the kustomize output | only jobs-mcp's `${N8N_TAILNET_FQDN}` in the whole prod build | **✅ 2026-09-10**: base output clean; the prod build's one placeholder is jobs-mcp's; CI test scans the base's YAML |
| §13-15 Console tier offers workspace + workspace-scoped key + workspace spend limit | yes | — (prerequisite 1) |
| `service_tier: "standard_only"` and `output_config.{effort,format}` accepted by name | yes (SDK 1.4 field names) | — (First live call steps 1 and 3) |
| does `count_tokens` count `output_config` grammar tokens | unknown; the byte heuristic counts the schema | — (step 3: `settled ≤ reserved` or `GatewaySettleOverReserve`) |
| `usage.inference_geo` on the first live call | absent / non-US ⇒ multiplier stays 100; US-pinned ⇒ set `BILLED_PRICE_MULTIPLIER_PCT=110` | — (step 1 row) |
| workspace spend-limit error shape | a 429 without `retry-after`, a 403 `billing_error`, or a 400 — the code maps the first two to `E_LANE_UNAVAILABLE` and reads a 400's message for spend-limit markers | — (step 4, `evt=lane_down`) |
| refusal `stop_reason` mapping | `refusal` ⇒ `finish_reason: content_filter`, still settled from usage | — (opportunistic) |
| provider `request-id` header lands in the row | yes | — (step 1 row, `provider_request_id`) |

## Rebuild from scratch (PVC lost)

The ledger is enforcement state, not a cache (spec §1): a fresh PVC boots
empty and **reopens every period budget and every job-cap pin** — the month's
spend so far is forgotten by the gateway and bounded only by the daily
brake and the Console workspace limit until the month turns. Record the
loss in STATUS; consider lowering caps for the rest of the month. Litestream
to the NAS is the named DR seam (needs a NAS user — appliance-tier).

## MERGE GATE (slice 3 — `gateway-smoke` task type) — not built

Pointer only (spec §8, §14): slice 2 live and `/readyz` 200 over the
tailnet; `n8n-executor` token in the SM map (done here) AND the n8n env;
`gateway-smoke` workflow imported ACTIVE through the workbench key;
executor proven directly with the webhook secret. Its own PR.
