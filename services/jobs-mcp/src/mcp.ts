import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import type { Queue, JobRow } from "./queue.js";
import type { Registry } from "./registry.js";
import type { Config } from "./config.js";
import type { Metrics } from "./metrics.js";
import { validateEnvelope } from "./envelope.js";
import { JobsError } from "./errors.js";

// MCP tool surface (spec §2/§3): enqueue, status, artifacts, cancel.
//
// Deliberately built on the low-level Server API, NOT registerTool: the
// high-level SDK wraps inputSchema in a zod object that silently STRIPS
// unknown fields and pre-empts our error taxonomy with -32602 protocol
// errors. Spec §3 requires the opposite (unknown fields ⇒ E_SCHEMA, every
// error ⇒ {code, message, retryable}), so validateEnvelope must be the ONE
// validator and must see the caller's arguments verbatim.
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

const ENVELOPE_JSON_SCHEMA = {
  type: "object",
  properties: {
    v: { type: "number", description: "envelope version; defaults to 1" },
    task_type: { type: "string" },
    payload: { type: "object" },
    budget_cap: { type: "number", description: "REQUIRED. USD ceiling; 0 = no model spend permitted. Never defaulted." },
    priority: { type: "integer", minimum: 0, maximum: 9, description: "0 most urgent; default 5" },
    artifacts_out: { type: "array", items: { type: "string" }, description: "declared output paths, relative; may be empty" },
    idempotency_key: { type: "string" },
  },
  required: ["task_type", "payload", "budget_cap", "artifacts_out"],
  additionalProperties: false,
} as const;

const ID_JSON_SCHEMA = {
  type: "object",
  properties: { id: { type: "string" } },
  required: ["id"],
  additionalProperties: false,
} as const;

const TOOLS = [
  {
    name: "enqueue",
    description:
      "Enqueue a job envelope. budget_cap (USD) is REQUIRED — 0 means no model spend permitted; a missing cap is an error, never default-to-unlimited.",
    inputSchema: ENVELOPE_JSON_SCHEMA,
  },
  { name: "status", description: "Get the full status of a job by id.", inputSchema: ID_JSON_SCHEMA },
  {
    name: "artifacts",
    description: "List a job's artifacts: names and scp-style URIs on the bulk NAS. Listings only, never content.",
    inputSchema: ID_JSON_SCHEMA,
  },
  {
    name: "cancel",
    description:
      "Cancel a QUEUED job. Running jobs cannot be stopped (the n8n public API has no stop endpoint); their timeout bounds the damage.",
    inputSchema: ID_JSON_SCHEMA,
  },
];

function requireId(args: Record<string, unknown> | undefined): string {
  if (!args || typeof args.id !== "string") throw new JobsError("E_SCHEMA", "id (string) is required");
  return args.id;
}

export function buildMcpServer(
  queue: Queue,
  registry: Registry,
  config: Config,
  metrics: Metrics,
  log: (line: Record<string, unknown>) => void = () => {},
): Server {
  const server = new Server({ name: "jobs-mcp", version: "0.1.0" }, { capabilities: { tools: {} } });

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const args = req.params.arguments as Record<string, unknown> | undefined;
    try {
      switch (req.params.name) {
        case "enqueue": {
          const envelope = validateEnvelope(args ?? {}, registry, config.maxBudgetCapUsd);
          const result = queue.enqueue(envelope);
          metrics.enqueued.labels(envelope.task_type).inc();
          if (!result.idempotent_replay) {
            // One line per enqueue (spec §9); never payload contents.
            log({ evt: "enqueue", job_id: result.id, task_type: envelope.task_type, priority: envelope.priority, budget_cap_usd: envelope.budget_cap });
          }
          return ok(result);
        }
        case "status": {
          const row = queue.get(requireId(args));
          if (!row) throw new JobsError("E_NOT_FOUND", "no such job");
          return ok(statusView(row));
        }
        case "artifacts": {
          const row = queue.get(requireId(args));
          if (!row) throw new JobsError("E_NOT_FOUND", "no such job");
          const dir = `${config.nasArtifactsBase}/jobs/${row.task_type}/${row.id}/`;
          const manifest = row.artifacts ? (JSON.parse(row.artifacts) as { name: string; bytes?: number; sha256?: string; undeclared?: boolean }[]) : [];
          return ok({
            id: row.id,
            state: row.state,
            artifacts_dir: dir,
            artifacts: manifest.map((a) => ({ ...a, uri: `${config.nasHost}:${dir}${a.name}` })),
          });
        }
        case "cancel": {
          const row = queue.cancel(requireId(args));
          metrics.transitions.labels("queued", "canceled").inc();
          log({ evt: "transition", job_id: row.id, task_type: row.task_type, from: "queued", to: "canceled" });
          return ok({ id: row.id, state: row.state });
        }
        default:
          throw new JobsError("E_SCHEMA", `unknown tool: ${req.params.name}`);
      }
    } catch (err) {
      return toolError(err);
    }
  });

  return server;
}
