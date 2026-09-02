# jobs-mcp

> **Alpha, and mostly AI-written.** This service was spec'd, implemented, and
> adversarially reviewed by Claude agent sessions under human direction, and
> runs here as a personal-infrastructure testbed while its operator kicks the
> tires. Expect sharp edges; interfaces and internals may change without
> notice. A polished, properly packaged open-source version will likely be a
> later rewrite — don't build on this one.

Task-queue MCP service of the personal cloud. Accepts job envelopes over MCP
(`enqueue`, `status`, `artifacts`, `cancel`), persists them in SQLite on a
local PVC, and dispatches them to deterministic executors — n8n workflows
reached over the tailnet with one synchronous webhook call each. Depends on:
tailscale-operator (exposure + n8n egress), external-secrets (bearer token,
n8n API key, webhook secret), Harbor (image). Job records live on its PVC;
artifact payloads live on the bulk NAS (`truenas-bulk-52tb`), written by
executors — this service holds no NAS credentials. Spec (approved):
`docs/specs/jobs-mcp.md`. Agentic executors and the model gateway plug in
later behind the same interface.
