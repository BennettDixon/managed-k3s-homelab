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
# ~/.zshrc on the workbench — value from the n8n UI, never committed
export N8N_API_KEY="..."
```

Claude Code expands `${N8N_API_KEY}` from the environment at MCP startup, so
rotating the key is: generate new key in UI → update the env var → restart
the session. Nothing in the repo changes.
