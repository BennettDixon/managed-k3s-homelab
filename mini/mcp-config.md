# Workbench MCP configuration (Mac mini / Claude Code)

Documented config only — **no secrets in this repo, ever**. Keys are
referenced by environment variable and live on the workbench (shell env or
keychain).

## n8n

The n8n instance runs in a tailnet-bound LXC at `http://n8n:5678` (MagicDNS
name; the service listens on the tailscale interface only, so it is
unreachable from the LAN or anywhere off-tailnet).

Add to the workbench MCP config (e.g. `~/.claude.json` `mcpServers`, or
`claude mcp add`):

```json
{
  "mcpServers": {
    "n8n": {
      "command": "npx",
      "args": ["-y", "n8n-mcp"],
      "env": {
        "N8N_API_URL": "http://n8n:5678",
        "N8N_API_KEY": "${N8N_API_KEY}"
      }
    }
  }
}
```

`N8N_API_KEY`: generate in the n8n UI (Settings → n8n API → Create API key)
after first-run setup, then export it in the workbench shell profile:

```bash
# ~/.zshenv on the workbench — value from the n8n UI, never committed
export N8N_API_KEY="..."
```

Claude Code expands `${N8N_API_KEY}` from the environment at MCP startup, so
rotating the key is: generate new key in UI → update the env var → restart
the session. Nothing in the repo changes.

## jobs-mcp

The task queue itself (live since 2026-09-01) — registered on both workbench
accounts (`bennett` and `agent`) as a streamable-HTTP server:

```bash
claude mcp add --transport http --scope user jobs-mcp http://jobs-mcp/mcp \
  --header 'Authorization: Bearer ${JOBS_MCP_TOKEN}'
```

`JOBS_MCP_TOKEN` lives in each account's `~/.zshenv` (same value as the
`jobs_mcp_bearer_token` terraform var; rotate in tfvars → targeted apply →
update the env). Tools: `enqueue` (budget_cap REQUIRED, 0 = no model spend),
`status`, `artifacts`, `cancel` (queued jobs only). Contract:
`docs/specs/jobs-mcp.md` §3.

## knowledge-mcp

The retrieval service (manifests slice 2, 2026-09-02) — registered on the
`bennett` workbench account as a streamable-HTTP server with the OPERATOR
caller token:

```bash
claude mcp add --transport http --scope user knowledge-mcp http://knowledge-mcp/mcp \
  --header 'Authorization: Bearer ${KNOWLEDGE_MCP_TOKEN}'
```

`KNOWLEDGE_MCP_TOKEN` lives in `~/.zshenv` (the `operator` entry of the
caller-token map — terraform var `knowledge_mcp_operator_token`; rotate in
tfvars → targeted apply → pod restart → update the env, see
`docs/runbooks/knowledge-mcp.md`). Tools: `search`, `fetch`, `ingest`,
`reingest`, `list_corpora` (spec §3). The `agent` account gets its own caller
id (one more map key + `callers:` line) rather than the operator token when
it needs access; NanoClaw is never registered against this token — it gets a
`frontend`-class id and a curated corpus (spec SIGN-OFF 3).
