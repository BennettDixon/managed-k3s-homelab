# knowledge-mcp — v1 specification

**Status: APPROVED 2026-09-02** (panel-designed 2026-09-01: four designers
with opposed optimization targets, two adversarial critics; implementation
claims bench-verified against the house SQLite binding and the live cluster;
review artifact linked from STATUS). Operator sign-offs, recorded where they
bind as **[SIGN-OFF n]**: (1) pinned pod fetch — approved; (2) caller-token
JSON map — delegated to engineering, map chosen ("get it right to start" —
avoids the breaking scalar→map secret reshape later); (3) homelab-notes
operator-only, NanoClaw gets a curated corpus later — approved; (4) first
working CI job rides slice 1, broken kubeval workflow deleted — approved.

**README paragraph (repo convention):** `knowledge-mcp` is the retrieval MCP
service of the personal cloud. It answers `search` and `fetch` over registered
corpora and keeps those corpora current via `ingest`/`reingest`, backed by one
SQLite+FTS5 file on a PVC. Corpus #1 is this repo's own operational notes
(docs/ + proxmox/). Depends on: tailscale-operator (exposure), external-secrets
(caller tokens), Harbor (image), and jobs-mcp (scheduled re-ingest rides a
`knowledge-reingest` task). Vector search, NanoClaw access, and non-git corpora
are seams (§11), not features.

**Design stance.** Same failure model as jobs-mcp: power dies mid-write, any
box, any moment; every state transition is one SQLite transaction; restart is
the recovery mechanism. One deliberate difference in honesty: for corpus #1 and
today's primary client (Claude sessions on a workbench that already holds a
full checkout), BM25-over-MCP adds almost nothing over grep — **v1's product is
the skeleton** (corpus governance, caller classes, provenance labels, eval
harness) exercised while stakes are zero, plus service to clients *without*
checkouts (n8n routines, roaming devices, future workers, NanoClaw). A quiet v1
is not failure; corpus #2 (clippings, papers) is the adoption test. The load-
bearing invariant everywhere: **the index is a cache** — rebuildable from a
declared external source of truth, so placement moves, tokenizer changes, and
store migrations are re-ingests, never data migrations.

## 1. Backing store

SQLite, single file `/data/knowledge.db`, `journal_mode=WAL`,
`synchronous=FULL`, on PVC `knowledge-mcp-data` (1Gi, `local-path`, RWO).
Measured: corpus #1 (8 files, 54,552 bytes) indexes to **216 KiB** including
FTS5 — the 1Gi claim is ~4,600× headroom. Single writer is structural:
`replicas: 1`, `strategy: Recreate`, RWO (jobs-mcp §8 verbatim).

```sql
CREATE TABLE docs (
  doc_id        TEXT PRIMARY KEY,   -- "<corpus>:<repo-relative-path>"
  corpus        TEXT NOT NULL,
  uri           TEXT NOT NULL,      -- canonical source URI
  title         TEXT,               -- H1
  content       TEXT NOT NULL,      -- normalized markdown
  content_sha256 TEXT NOT NULL,
  bytes         INTEGER NOT NULL,
  source_commit TEXT,               -- git corpora: commit at ingest
  trust         TEXT NOT NULL,      -- denormalized from registry at ingest
  ingested_by   TEXT NOT NULL,      -- caller_id
  ingested_at   INTEGER NOT NULL,
  tombstoned_at INTEGER             -- reingest marks vanished docs
);
CREATE VIRTUAL TABLE chunks USING fts5(
  title, heading_path, body,        -- bm25 column weights 8.0 / 4.0 / 1.0
  doc_id UNINDEXED, chunk_index UNINDEXED, content_sha256 UNINDEXED,
  tokenize = 'unicode61'            -- NO stemming: identifier fidelity wins
);
```

Migrations are additive-only `CREATE … IF NOT EXISTS` at boot (jobs-mcp §1);
virtual-table creation (fts5 now, sqlite-vec `vec0` at S1) fits that rule. A
**tokenizer change is a rebuild, not a migration** — legal because the index is
a cache; the spec says so here so nobody treats it as a schema event.

**Rebuild-source admission rule (load-bearing):** every corpus declares
`rebuild_source` (`git` today; `nas` at S4). Ingest of content with no external
source of truth is rejected (`E_NO_REBUILD_SOURCE`) until the raw-archive-to-
NAS path exists — otherwise the first pushed clipping silently converts the
cache into the only copy. Backup story: **none, by design** — v1's only corpus
rebuilds from GitHub in under a minute. DR seam (named, §11-S5): a Litestream
read-replica on edgepve — compute is the *remote* site with the longest repair
tail, and a one-file store makes the replica purely additive.

**Vector-store decision (closes STATUS Parked):** sqlite-vec loaded in-process
into this same file at S1 — candidate (c). Verified: the house
better-sqlite3@11.10.0 build has `ENABLE_FTS5` and `loadExtension`; prebuilt
`linux-x64-gnu` loads on the `node:22-slim` (Debian) base image. Rejected: (a)
pgvector on edgepve — a real Postgres server on residential power, two secret
systems, and a cross-site query triangle (either-site-down kills search for
everyone) to serve ~10³ chunks; (b) LanceDB — a second storage engine whose FTS
is younger than FTS5, bought for a feature v1 doesn't ship. **Reopen tripwires
for (a), adopted as numbers: >250K chunks, OR a second non-MCP SQL consumer,
OR p95 search >500ms.** At ≤10⁴ vectors, brute-force cosine is exact and
sub-millisecond; no ANN index at this scale.

## 2. Transport, exposure, auth

- **Transport:** MCP Streamable HTTP, stateless, `POST /mcp`, port 8080 —
  jobs-mcp §2 verbatim (Node 22 pinned: better-sqlite3@11 ships no Node-24
  prebuilt and `-slim` cannot gyp-build). Express JSON body limit **512 KiB**,
  explicitly ≥ the largest per-corpus `max_doc_bytes` (jobs-mcp's cloned
  256 KB limit would reject legal ingest bodies otherwise).
- **Exposure:** ClusterIP + `tailscale.com/expose` + `hostname: knowledge-mcp`
  → `http://knowledge-mcp/mcp`. Compute-site placement (the only k8s node).
  The "serving → edge" rule cannot be honored today — no edge node exists;
  §11-S6 names both edge seams. Query cost for the edge workbench: one
  cross-site RTT (10–40 ms; DERP days ~250 ms) — noise against agent turns.
- **Auth [SIGN-OFF 2]:** one AWS SM secret `k3s_knowledge_mcp_caller_tokens`
  holding a **JSON map** `{"operator": "<token>", "n8n-reingest": "<token>"}`
  (terraform → ExternalSecret, jobs-mcp path). This deliberately diverges from
  jobs-mcp's scalar because v1 already has **two callers on day one** — the
  operator surfaces and the scheduled-reingest executor — and reshaping a
  scalar secret later touches the SM shape, the ExternalSecret, and every
  holder atomically, while the map now is ~20 lines. Declared the house target
  shape (jobs-mcp adopts it at its NanoClaw retrofit). Token → `{caller_id,
  class}` via constant-time compare in auth middleware on every request;
  policy (caller → class, class → tools/corpora) lives in the repo registry,
  never in the secret. Logs carry `caller_id` on every request and **never
  query text or document content** (queries will eventually contain
  correspondent text). Tailscale WhoIs augmentation is a named seam in the
  same middleware.

## 3. Tool contract

Five tools. Errors are `{code, message, retryable}` (jobs-mcp §3 taxonomy
style): `E_UNAUTHORIZED`, `E_FORBIDDEN` (tool not granted to caller class),
`E_SCHEMA`, `E_CORPUS_UNKNOWN`, `E_NOT_FOUND` (also returned for corpora
invisible to the caller — no existence oracle), `E_URI_FORBIDDEN` (allowlist
miss, never retryable), `E_URI_UNREACHABLE` (retryable), `E_DOC_TOO_LARGE`,
`E_QUERY_INVALID`, `E_NO_REBUILD_SOURCE`, `E_UNSUPPORTED`, `E_INTERNAL`.

- **`search(corpus, query, k?)`** — k default 6, cap 25. Returns
  `{corpus, retrieval: "bm25", index_as_of: {commit, ingested_at}, results:
  [{chunk_id, doc_id, path, heading_path, text, score, rank, trust,
  source_commit, neighbors: {prev, next}}]}`. Scores normalized positive
  (FTS5 `bm25()` is negative-is-better — verified). Neighbors are **ids, not
  text**: context spend stays visible to the caller. Response capped ~24 KiB
  (measured: k=6 of real chunks ≈ 8–10 KB ≈ 2–3K tokens — the per-call context
  cost a caller pays; stated here because MCP results land verbatim in agent
  context).
- **`fetch(doc_id, chunk_id?)`** — full normalized doc + metadata ≤ 64 KiB
  (jobs-mcp inline-result precedent); larger docs return metadata + chunk list
  and are fetched per-chunk. Corpus visibility re-checked server-side from the
  doc row (ids are guessable and never capability-bearing).
- **`ingest(corpus, uri)`** — synchronous, one document; see §6. Returns
  `{doc_id, status: "indexed"|"unchanged", content_sha256}`; `"queued"` is
  reserved in the enum so async ingest (S3) lands with no shape change. A
  `content` parameter is **declared but rejected** (`E_UNSUPPORTED`) — the
  push shape arrives additively at S3/S4 with the NAS-archive rule.
- **`reingest(corpus)`** — walks the corpus source (git tree listing), ingests
  changed docs, short-circuits unchanged (sha256), **tombstones vanished docs
  in the same transaction**. Idempotent; keyed on content hashes. This is the
  freshness mechanism, and the only thing the scheduled executor calls.
- **`list_corpora()`** — corpora visible to the caller (name, description,
  doc count, index_as_of). Discoverability without hardcoding.

Caller classes gate tools via a **deny-by-default capability table** resolved
before any handler runs (one dispatch choke point, never per-handler checks):
`operator: [search, fetch, ingest, reingest, list_corpora]`; `reingest-bot:
[reingest, list_corpora]`; `frontend` (future): `[search, fetch,
list_corpora]`. `tools/list` is also filtered per class (hygiene, not the
control — the dispatch gate is the control).

## 4. Corpus registry

Repo-owned ConfigMap `apps/base/knowledge-mcp/corpora.yaml` via
`configMapGenerator` — changes hash-roll the Recreate deployment (jobs-mcp §4
semantics: atomic, auditable, seconds of downtime; a boot-read ConfigMap would
go silently stale). Parse failure fails readiness. **Registry schema
validation lives in the vitest suite** — a malformed registry PR would
otherwise freeze the Flux chain (`wait: true`) with nothing in CI to catch it.

```yaml
corpora:
  homelab-notes:
    description: "repo docs/ + proxmox/ operational notes"
    rebuild_source: git                  # admission: required (§1)
    visibility: operator                 # operator | frontend
    trust: operator-authored             # operator-authored | curated | untrusted
    allowed_uri_prefixes:                # ingest hard-rejects anything else
      - https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/docs/
      - https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/proxmox/
    tree_source: https://api.github.com/repos/BennettDixon/managed-k3s-homelab/git/trees/main
    max_doc_bytes: 262144
callers:
  operator: { class: operator }
  n8n-reingest: { class: reingest-bot, corpora: [homelab-notes] }
```

Admission rules at parse time: `trust: untrusted` may never declare
`visibility: frontend` (quarantine is structural — a poisoned clipping cannot
reach a chat conduit by construction); every corpus requires `rebuild_source`;
`frontend` visibility requires `trust != untrusted` **and** an explicit PR (a
human reads what they promote). **[SIGN-OFF 3]** `homelab-notes` stays
`visibility: operator` — NanoClaw is later served a deliberately *curated*
corpus (e.g. `homelab-faq`, operator-authored) rather than the working notes,
which accrete non-public operational detail and would hand a conversational
exfiltration channel to hostile correspondents. The registry is public-repo
safe: GitHub URLs of this public repo, MagicDNS shortnames only, never ts.net
FQDNs or LAN IPs.

## 5. Retrieval

- **Chunking: one heading section per chunk**, split at `##`/`###` by a
  fence-aware state machine (~15 lines, no markdown dependency — measured on
  the real corpus: naive `^#` splitting yields 74 chunks with 12 false
  mid-fence splits from `proxmox/edgepve.md`'s bare fence containing column-0
  `#` comment lines; fence-aware yields 62). Never split inside fenced blocks
  or tables. Sections >~1,200 tokens split at paragraph bounds with the
  heading repeated; fragments <~200 chars merge forward. No overlap — chunks
  carry `prev`/`next` ids instead. Breadcrumb (`title > heading path`)
  prepended to indexed text and kept as metadata.
- **Identity:** `doc_id = <corpus>:<repo-relative-path>` (human-legible,
  citable; renames tombstone + re-create — accepted non-goal). `chunk_id =
  <doc_id>#<heading-slug-path>[~N]`, **declared unstable across doc edits**
  (ordinal shift when a duplicate heading is inserted); durable citation =
  `doc_id + heading_path + source_commit`.
- **Query handling:** server-built MATCH strings, always — raw hyphenated
  input (`jobs-mcp`) *always* errors in FTS5 (verified: column-filter
  parsing), so the server tokenizes, quotes every term, phrase-quotes
  identifier-shaped terms (`_-./:$`), then two passes: all-terms AND, OR
  fallback if empty. `unicode61`, no stemming — `jobs_bridge_up` tokenizes
  symmetrically (verified) so identifier queries exact-match while partial
  terms still hit. Ranking: `bm25(chunks, 8.0, 4.0, 1.0)` (title,
  heading_path, body). Fusion for S1 pinned now: RRF k=60 behind an internal
  `rank(query, candidates)` interface with one v1 implementation.
- **Eval:** `services/knowledge-mcp/eval/golden.yaml`, ~20 queries authored
  from operator questions (not from index behavior), including 2–3 known-hard
  paraphrases marked `expect: miss` so the lexical blind spot stays on a
  scoreboard. Metrics: recall@5, MRR; `npm run eval` builds a scratch index of
  HEAD. Standing rule: every "search missed X" fix adds its golden query.
  **[SIGN-OFF 4]** whether eval runs in CI (this repo's only workflow is
  vestigial — its glob matches a directory that doesn't exist) or stays a
  local script + runbook line.

## 6. Ingest and freshness

**Shape [SIGN-OFF 1]: inline, synchronous, in-pod fetch — pinned to registry
prefixes.** One doc per call (MCP SDK client timeout is 60 s — verified;
whole-corpus-in-one-call is forbidden by spec). Flow: auth → corpus visibility
→ URI prefix allowlist (https only, redirects disabled, resolve-and-pin
against rebinding, 10 s timeout, `max_doc_bytes` cap) → fetch → normalize
(strip control chars and Unicode tag/invisible codepoints — hidden-instruction
smuggling) → sha256 short-circuit (`"unchanged"`) → fence-aware chunk → one
transaction (upsert doc + delete/insert chunks). Client abort mid-call is
harmless: the transaction completes server-side; retry is a sha-keyed no-op.

Why in-pod fetch rather than the alternatives the panel fought over: the RWO
single-writer means every path terminates in this pod anyway; routing through
jobs-mcp/n8n would add a registry entry, a workflow export, a webhook secret,
a **new inbound n8n→pod direction with a new ACL ask**, and a chunker exiled
into an untestable workflow Code node (and the n8n LXC has no git — verified);
push-only ingest deletes the fetch surface but recreates the
manual-step-that-rots (its scheduler needs a caller token anyway, buying no
auth simplification). Residual risk accepted with eyes open: the pod fetches
from GitHub's raw/API endpoints — the same trust root Flux already pulls this
repo from, so no *new* trust anchor; the redirect/rebinding hardening is
implementation, not assertion. **Pre-implementation check (jobs-mcp §6
discipline):** a scratch pod GETs `https://raw.githubusercontent.com/…` →
expect 200 (the class is proven — the external-secrets pod syncs AWS SM over
pod-network HTTPS today; verified live). Fallback if it fails: the `content`
push shape unlocks (already declared in the contract).

**Freshness:** `reingest(corpus)` walks `tree_source`, upserts changed docs,
tombstones vanished ones — scheduled by a **`knowledge-reingest` jobs-mcp
task_type** (deterministic, idempotent, `budget_cap: 0` — the invariant rides
the existing envelope) whose n8n executor's *only* action is calling
`reingest` with the scoped `n8n-reingest` token. The executor fetches
nothing, parses nothing, holds no content; n8n compromise's knowledge blast
radius is "can re-index the public repo" — nil. Nightly schedule + manual
enqueue after doc-heavy merges. Registry entry + workflow export ship in one
PR (n8n/ convention).

**Budget discipline pre-gateway:** v1 spends $0 **by construction** — no
embedding path exists, no third-party API key is minted, and the only
model-spend-capable hop (the jobs envelope) carries `budget_cap: 0`. When
embeddings land (S2): local CPU backend keeps `budget_cap: 0` legal; an API
backend refuses to run without an explicit cap (`E_BUDGET_CAP_MISSING`) and
the key lives **executor-side, never in this pod** — real enforcement arrives
with the gateway, and this spec does not pretend otherwise.

## 7. Injection posture (results are the attack surface)

You cannot sanitize injection out of text; you can guarantee nothing crosses
the wire unlabeled. Enforcement points, all in the response serializer (not
convention): every result carries machine-readable `{corpus, trust, source,
ingested_at}`; content from `trust != operator-authored` corpora ships inside
a delimited envelope with an explicit untrusted-content header; snippets are
capped; results are text-only (active content stripped at ingest). Structured
JSON fields only — never a server-concatenated markdown blob. The cross-service
loop is named: an agent holding both knowledge and jobs tokens can be steered
by a poisoned result toward `enqueue`; jobs-mcp's `frontend_allowed` fence and
mandatory `budget_cap` bound that damage — both invariants stay. Seam:
instruction-likeness scanning at ingest feeding an operator review queue.

## 8. Kubernetes shape and Flux wiring

Clone of jobs-mcp §8 with one deliberate deviation:

- Deployment `replicas: 1`, `strategy: Recreate`, RWO PVC; image
  `harbor.internal/knowledge/knowledge-mcp:<semver>` (per-app Harbor project +
  pull robot); resources `50m/128Mi` → `250m/256Mi`; env via ExternalSecrets
  in the base — legal here for the same reason as jobs-mcp: **this base is
  not added to `apps/development`** (the alerts-wiring review established the
  distinction: kube-prometheus-stack's base ships to dev, jobs-mcp's doesn't).
- **Probes — the deviation:** liveness `GET /healthz` (process + DB open,
  never writes). Readiness `GET /readyz` = **SELECT ping + registry parsed +
  token map loaded — no write-ping** (jobs-mcp write-pings because it is
  write-accepting; here a full disk must degrade *ingest*, not take *search*
  down — read-only serving is the service's point). Write health is an
  ingest-time error path + `knowledge_ingest_errors_total` metric.
- **MERGE GATE (runbook carries it):** SM entry `k3s_knowledge_mcp_caller_tokens`
  (terraform, targeted apply), Harbor `knowledge` project + robot + image
  pushed, registry ConfigMap parseable — all before the base joins
  `apps/homelab-prod/kustomization.yaml`. The `knowledge-reingest` task_type
  PR additionally gates on: workflow imported + active, `n8n-reingest` token
  present in both the SM map and the n8n env.

## 9. Observability

`GET /metrics` + ServiceMonitor labeled `release: kube-prometheus-stack` on a
Service carrying the selected label (the twice-hit trap, §9 of the jobs spec).
Metrics: `knowledge_search_total{corpus}`, `knowledge_search_duration_seconds`,
`knowledge_ingest_total{corpus, result}`, `knowledge_ingest_errors_total`,
`knowledge_docs{corpus}`, `knowledge_chunks{corpus}`, `knowledge_db_bytes`,
`knowledge_index_age_seconds{corpus}`. PrometheusRule ships in the same PR:
`KnowledgeMcpMetricsAbsent` (absent-guard — instant-vector alerts silently
disarm when the target vanishes), `KnowledgeDbOversized`
(`knowledge_db_bytes > 0.8 × 1Gi`), `KnowledgeIndexStale`
(`knowledge_index_age_seconds > 172800` — reingest has been failing for two
days). **Deliberately no node-fs disk alert** — `kubelet_volume_stats` under
local-path reports the node filesystem, jobs-mcp's `JobsPvcDiskFilling`
already owns that signal, and a clone would double-fire at node-80%. Delivery
rides the Alertmanager → n8n receiver (PR #6); until that merges, rules fire
visibly in Prometheus only.

## 10. Failure matrix

| Failure | Outcome | Mechanism |
|---|---|---|
| Power cut mid-ingest | Previous doc version intact | One transaction per doc |
| Crash/deploy mid-reingest | Partial progress kept; re-run is a no-op for done docs | Per-doc transactions + sha short-circuit |
| GitHub unreachable | Ingest/reingest error (retryable); search/fetch unaffected | Fetch isolated from serving path |
| Disk full | Ingest errors cleanly; **search keeps serving**; liveness green | No-write readyz deviation (§8) |
| jobs-mcp or n8n down | Freshness pauses; nothing else | Scheduler is outside the serving path |
| Registry PR malformed | Readiness fails, chain freezes | configMapGenerator + parse-fails-readyz; vitest schema check is the guard |
| PVC lost | Rebuild from git in minutes | Index-is-cache + rebuild-source rule |
| Compute site down | Service down for all clients; workbench falls back to grep on its checkout | Single-node reality; DR seam §11-S5 |

## 11. Seams and non-goals

- **S1 — Vectors/hybrid:** nullable `embedding BLOB` + `embedding_model` per
  chunk (additive); sqlite-vec `vec0` virtual table in the same file
  (loadExtension verified available); candidate model `bge-small-en-v1.5`
  (384d, ONNX int8, ~35MB; corpus embeds in <1 min on 2 cores); RRF k=60
  behind the pinned `rank()` interface; re-embed = `embedding_model != current
  OR content_hash changed`. Gated on golden-eval evidence, not vibes.
- **S2 — Embedding spend:** local backend first (`budget_cap: 0`); API backend
  refuses without explicit cap; key executor-side only; true enforcement =
  gateway (build order 4).
- **S3 — Async/queued ingest:** `status: "queued"` already reserved; ingest
  flips to enqueue a jobs-mcp task when work stops being sub-second.
- **S4 — Non-git corpora (clippings, papers):** the `content` push param
  unlocks with the NAS-archive rule — raw originals land on
  `truenas-bulk-52tb` (dataset creation = operator ask) *before* indexing;
  `rebuild_source: nas`. URI fetch for non-repo hosts arrives only with the
  worker executor (hostile-input parsing belongs on a cattle box, not in the
  token-holding pod).
- **S5 — DR / edge read-replica:** Litestream sidecar → edgepve serves a
  read-only `knowledge-edge`; contract/schema/DNS unchanged.
- **S6 — Edge serving proper:** k3s agent node on edgepve + site labels
  (build-order item 1) → `nodeSelector` + reindex onto edge NVMe class.
- **S7 — NanoClaw onboarding:** SM map entry + `callers:` line + `frontend`
  class + its tagged-identity ACL grant (operator console task) + optionally a
  separate read-only Service pinned by ACL. Curated corpus per [SIGN-OFF 3].
- **S8 — WhoIs, instruction-likeness scanning, per-caller rate limits:** named
  slots in middleware/ingest/serializer respectively.
- **Non-goals v1:** vectors; embeddings; any non-git corpus; NAS access of any
  kind (zero NAS credentials, jobs-mcp stance); deletion tools (destructive —
  explicit human task; tombstones are soft); multi-replica; WhoIs; NanoClaw
  access; cross-corpus search.

## 12. Slices

1. **Code + tests** (`services/knowledge-mcp/`): server, auth middleware +
   caller map, registry parser + schema validation, fence-aware chunker,
   FTS5 store + query builder, search/fetch/ingest/reingest/list_corpora,
   eval harness + golden.yaml. No manifests, no infra, no NAS. (This slice is
   Step 3 of the session plan, gated on this spec's approval.)
2. **Manifests + secrets**: base + prod wiring, ExternalSecret, ServiceMonitor,
   PrometheusRule, README; terraform SM entry; Harbor project/robot; MERGE
   GATE per §8.
3. **`knowledge-reingest` task_type**: registry entry + n8n workflow export +
   scoped token + end-to-end proof (`enqueue → reingest → index_as_of
   advances`); runbook `docs/runbooks/knowledge-mcp.md`.
