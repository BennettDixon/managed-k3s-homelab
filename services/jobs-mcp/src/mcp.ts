import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { Queue, JobRow } from "./queue.js";
import type { Registry } from "./registry.js";
import type { Config } from "./config.js";
import type { Metrics } from "./metrics.js";
import { validateEnvelope } from "./envelope.js";
import { JobsError } from "./errors.js";

// MCP tool surface (spec §2/§3): enqueue, status, artifacts, cancel.
// Every tool error is the taxonomy shape {code, message, retryable}.
function toolError(err: unknown) {
  const e = err instanceof JobsError ? err : new JobsError("E_INTERNAL", "internal error", true);
  return { content: [{ type: "text" as const, text: JSON.stringify(e.toJSON()) }], isError: true };
}

function ok(payload: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(payload) }] };
}

// status(id) returns every field in every state, nulls where unknown, so
// clients never branch on shape (spec §5).
function statusView(row: JobRow) {
  return {
    id: row.id,
    task_type: row.task_type,
    state: row.state,
    priority: row.priority,
    attempts: row.attempts,
    max_attempts: row.max_attempts,
    budget_cap_usd: row.budget_cap_usd,
    spent_usd: row.spent_usd,
    enqueued_at: row.enqueued_at,
    started_at: row.started_at,
    finished_at: row.finished_at,
    error: row.error ? JSON.parse(row.error) : null,
    result: row.result ? JSON.parse(row.result) : null,
    artifact_count: row.artifacts ? (JSON.parse(row.artifacts) as unknown[]).length : null,
    idempotent_replay: null,
  };
}

export function buildMcpServer(queue: Queue, registry: Registry, config: Config, metrics: Metrics): McpServer {
  const server = new McpServer({ name: "jobs-mcp", version: "0.1.0" });

  server.registerTool(
    "enqueue",
    {
      description:
        "Enqueue a job envelope. budget_cap (USD) is REQUIRED — 0 means no model spend permitted; a missing cap is an error, never default-to-unlimited.",
      inputSchema: {
        v: z.number().optional(),
        task_type: z.string(),
        payload: z.record(z.unknown()),
        budget_cap: z.number(),
        priority: z.number().optional(),
        artifacts_out: z.array(z.string()),
        idempotency_key: z.string().optional(),
      },
    },
    async (args) => {
      try {
        const envelope = validateEnvelope(args, registry, config.maxBudgetCapUsd);
        const result = queue.enqueue(envelope);
        metrics.enqueued.labels(envelope.task_type).inc();
        return ok(result);
      } catch (err) {
        return toolError(err);
      }
    },
  );

  server.registerTool(
    "status",
    { description: "Get the full status of a job by id.", inputSchema: { id: z.string() } },
    async ({ id }) => {
      try {
        const row = queue.get(id);
        if (!row) throw new JobsError("E_NOT_FOUND", `no such job: ${id}`);
        return ok(statusView(row));
      } catch (err) {
        return toolError(err);
      }
    },
  );

  server.registerTool(
    "artifacts",
    {
      description: "List a job's artifacts: names and scp-style URIs on the bulk NAS. Listings only, never content.",
      inputSchema: { id: z.string() },
    },
    async ({ id }) => {
      try {
        const row = queue.get(id);
        if (!row) throw new JobsError("E_NOT_FOUND", `no such job: ${id}`);
        const dir = `${config.nasArtifactsBase}/jobs/${row.task_type}/${row.id}/`;
        const manifest = row.artifacts ? (JSON.parse(row.artifacts) as { name: string; bytes?: number; sha256?: string; undeclared?: boolean }[]) : [];
        return ok({
          id: row.id,
          state: row.state,
          artifacts_dir: dir,
          artifacts: manifest.map((a) => ({ ...a, uri: `${config.nasHost}:${dir}${a.name}` })),
        });
      } catch (err) {
        return toolError(err);
      }
    },
  );

  server.registerTool(
    "cancel",
    {
      description: "Cancel a QUEUED job. Running jobs cannot be stopped (the n8n public API has no stop endpoint); their timeout bounds the damage.",
      inputSchema: { id: z.string() },
    },
    async ({ id }) => {
      try {
        const row = queue.cancel(id);
        return ok({ id: row.id, state: row.state });
      } catch (err) {
        return toolError(err);
      }
    },
  );

  return server;
}
