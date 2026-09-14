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
`docs/runbooks/knowledge-mcp.md`). Never paste the literal token into
`claude mcp add`: it would land in shell history AND in plaintext
`~/.claude.json`. Keep the single-quoted `${KNOWLEDGE_MCP_TOKEN}` reference;
a 401 from the server usually means the variable was unset when the MCP
server started (`echo ${KNOWLEDGE_MCP_TOKEN:+set}`). Tools: `search`, `fetch`, `ingest`,
`reingest`, `list_corpora` (spec §3). The `agent` account gets its own caller
id (one more map key + `callers:` line) rather than the operator token when
it needs access; NanoClaw is never registered against this token — it gets a
`frontend`-class id and a curated corpus (spec SIGN-OFF 3).

## gateway

The model gateway (live since 2026-09-14, slice 2) is not an MCP server: it
is an OpenAI-compatible HTTP endpoint at `http://gateway/v1` (tailnet-only)
under a per-project budget ledger. Spec `docs/specs/gateway.md`; runbook
`docs/runbooks/gateway.md`.

The operator caller token (the `operator` entry of the caller-token map,
terraform var `gateway_operator_token`) lives ONLY in the `bennett`
account's `~/.zshenv`, under its own name:

```bash
# ~/.zshenv on the workbench (bennett account only) — value from terraform.tfvars, never committed
export GATEWAY_TOKEN="..."
```

**Never export it as `OPENAI_API_KEY`.** Every OpenAI-speaking tool without
an `OPENAI_BASE_URL` override would send it to api.openai.com. A tool that
should use the gateway gets both settings together, in that tool's own
config, plus the two gateway headers every request needs:

| setting | value |
|---|---|
| `OPENAI_BASE_URL` | `http://gateway/v1` |
| `OPENAI_API_KEY` | `$GATEWAY_TOKEN`, per tool |
| header `X-Gateway-Project` | `homelab-ops` (or `gateway-smoke` for tests) |
| header `X-Gateway-Budget-Cap-USD` | the request's cap, required, no default; `0` refuses |

The operator token's ceilings are in `apps/base/gateway/registry.yaml`: $5
per request and $5 billed per UTC day. The `agent` account gets its own
caller id (one more map key + a `callers:` line) when it first needs the
gateway, never this token. NanoClaw is never a gateway caller. Rotation:
tfvars, targeted apply, ESO force-sync, pod delete, then update
`~/.zshenv` (runbook "Rotation").
