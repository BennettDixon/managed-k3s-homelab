import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { z } from "zod";

// Task-type registry (spec §4). Parsed at startup; parse failure fails
// readiness. Admission rules enforced here, not at enqueue time.
const TaskTypeSchema = z.object({
  executor: z.literal("n8n"), // "worker" reserved for v2 (spec §11)
  webhook_path: z.string().regex(/^[a-z0-9][a-z0-9/-]*$/),
  timeout_s: z.number().int().positive().max(3600),
  max_attempts: z.number().int().positive().max(10),
  idempotent: z.boolean(),
  frontend_allowed: z.boolean().default(false),
  payload_schema: z.record(z.unknown()).optional(),
});

const RegistrySchema = z.object({
  task_types: z.record(z.string().regex(/^[a-z0-9][a-z0-9-]{1,62}$/), TaskTypeSchema),
});

export type TaskTypeEntry = z.infer<typeof TaskTypeSchema>;
export type Registry = Map<string, TaskTypeEntry>;

export function parseRegistry(yamlText: string): Registry {
  const parsed = RegistrySchema.parse(parse(yamlText));
  const registry: Registry = new Map();
  for (const [name, entry] of Object.entries(parsed.task_types)) {
    if (entry.executor === "n8n" && !entry.idempotent) {
      throw new Error(`registry admission: task_type "${name}" has executor n8n and MUST declare idempotent: true (spec §4)`);
    }
    registry.set(name, entry);
  }
  return registry;
}

export function loadRegistry(path: string): Registry {
  return parseRegistry(readFileSync(path, "utf8"));
}
