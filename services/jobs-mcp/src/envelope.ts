import { createHash } from "node:crypto";
import { JobsError } from "./errors.js";
import type { Registry } from "./registry.js";

// Envelope validation (spec §3). Runs entirely at enqueue; a rejected
// envelope creates no row. Unknown top-level fields are rejected.
export interface Envelope {
  v: number;
  task_type: string;
  payload: Record<string, unknown>;
  budget_cap: number;
  priority: number;
  artifacts_out: string[];
  idempotency_key?: string;
}

const KNOWN_FIELDS = new Set(["v", "task_type", "payload", "budget_cap", "priority", "artifacts_out", "idempotency_key"]);
const TASK_TYPE_RE = /^[a-z0-9][a-z0-9-]{1,62}$/;
const ARTIFACT_PATH_RE = /^[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+)*$/;
const MAX_JSON_BYTES = 64 * 1024;

export function validateEnvelope(raw: unknown, registry: Registry, maxBudgetCapUsd: number): Envelope {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new JobsError("E_SCHEMA", "envelope must be a JSON object");
  }
  const obj = raw as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (!KNOWN_FIELDS.has(key)) throw new JobsError("E_SCHEMA", `unknown envelope field: ${key}`);
  }

  const v = obj.v === undefined ? 1 : obj.v;
  if (v !== 1) throw new JobsError("E_ENVELOPE_VERSION", `unsupported envelope version: ${String(v)}`);

  const taskType = obj.task_type;
  if (typeof taskType !== "string" || !TASK_TYPE_RE.test(taskType)) {
    throw new JobsError("E_SCHEMA", "task_type is required and must match ^[a-z0-9][a-z0-9-]{1,62}$");
  }
  if (!registry.has(taskType)) {
    throw new JobsError("E_TASK_TYPE_UNKNOWN", `task_type not in registry: ${taskType}`);
  }

  const payload = obj.payload;
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new JobsError("E_PAYLOAD_INVALID", "payload is required and must be an object");
  }
  if (Buffer.byteLength(JSON.stringify(payload), "utf8") > MAX_JSON_BYTES) {
    throw new JobsError("E_PAYLOAD_INVALID", "payload exceeds 64 KiB");
  }

  // budget_cap: required, scalar USD, no default, ever (spec §3).
  if (!("budget_cap" in obj) || obj.budget_cap === null || obj.budget_cap === undefined) {
    throw new JobsError("E_BUDGET_CAP_MISSING", "budget_cap is required; there is no default");
  }
  const cap = obj.budget_cap;
  if (typeof cap !== "number" || !Number.isFinite(cap) || cap < 0) {
    throw new JobsError("E_BUDGET_CAP_INVALID", "budget_cap must be a finite USD number >= 0");
  }
  if (cap > maxBudgetCapUsd) {
    throw new JobsError("E_BUDGET_CAP_INVALID", `budget_cap exceeds MAX_BUDGET_CAP_USD (${maxBudgetCapUsd})`);
  }

  let priority = 5;
  if (obj.priority !== undefined) {
    if (typeof obj.priority !== "number" || !Number.isInteger(obj.priority) || obj.priority < 0 || obj.priority > 9) {
      throw new JobsError("E_SCHEMA", "priority must be an integer 0-9");
    }
    priority = obj.priority;
  }

  const artifactsOut = obj.artifacts_out;
  if (!Array.isArray(artifactsOut)) {
    throw new JobsError("E_SCHEMA", "artifacts_out is required (may be an empty array)");
  }
  if (artifactsOut.length > 32) throw new JobsError("E_SCHEMA", "artifacts_out exceeds 32 entries");
  for (const p of artifactsOut) {
    if (typeof p !== "string" || !ARTIFACT_PATH_RE.test(p)) {
      throw new JobsError("E_SCHEMA", `artifacts_out path invalid: ${String(p)}`);
    }
    // Explicit checks on top of the regex (spec §3): no ".." segment, no leading "/".
    if (p.startsWith("/") || p.split("/").includes("..")) {
      throw new JobsError("E_SCHEMA", `artifacts_out path escapes the job directory: ${p}`);
    }
  }

  let idempotencyKey: string | undefined;
  if (obj.idempotency_key !== undefined) {
    if (typeof obj.idempotency_key !== "string" || obj.idempotency_key.length < 8 || obj.idempotency_key.length > 128) {
      throw new JobsError("E_SCHEMA", "idempotency_key must be a string of 8-128 chars");
    }
    idempotencyKey = obj.idempotency_key;
  }

  const envelope: Envelope = {
    v: 1,
    task_type: taskType,
    payload: payload as Record<string, unknown>,
    budget_cap: cap,
    priority,
    artifacts_out: artifactsOut as string[],
  };
  if (idempotencyKey !== undefined) envelope.idempotency_key = idempotencyKey;
  return envelope;
}

// Stable stringify (sorted keys, recursive) so idempotency conflict detection
// is insensitive to JSON key order.
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

export function envelopeHash(e: Envelope): string {
  const canonical = stable({
    v: e.v,
    task_type: e.task_type,
    payload: e.payload,
    budget_cap: e.budget_cap,
    priority: e.priority,
    artifacts_out: e.artifacts_out,
  });
  return createHash("sha256").update(canonical).digest("hex");
}
