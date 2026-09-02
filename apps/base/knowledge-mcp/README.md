# knowledge-mcp (manifests)

Retrieval MCP service of the personal cloud: `search`/`fetch`/`ingest`/
`reingest`/`list_corpora` over MCP at `http://knowledge-mcp/mcp` (tailnet-only
via the tailscale operator), one SQLite+FTS5 file on `knowledge-mcp-data`
(local-path PVC — a rebuildable cache, never a source of truth). Corpus #1 is
this repo's own docs/ + proxmox/ notes, registered in `corpora.yaml`
(configMapGenerator: a registry change hash-rolls the Deployment). Depends on:
external-secrets (the caller-token JSON map + the Harbor pull robot, both
ExternalSecrets in this base), the Harbor `knowledge` project for image pulls,
and — for scheduled freshness — jobs-mcp's `knowledge-reingest` task (slice 3).
Prod-only: this base joins `apps/homelab-prod` and must never be added to
`apps/development`. Source: `services/knowledge-mcp/`. Spec:
`docs/specs/knowledge-mcp.md` (approved 2026-09-02). Operator runbook with the
MERGE GATE: `docs/runbooks/knowledge-mcp.md`.
