import Database from "better-sqlite3";
import { openDb } from "../src/db.js";
import { parseRegistry, type Registry } from "../src/registry.js";
import { Queue } from "../src/queue.js";
import type { Config } from "../src/config.js";

export function testDb(): Database.Database {
  return openDb(":memory:");
}

export function testRegistry(overrides = ""): Registry {
  return parseRegistry(`
task_types:
  smoke-heartbeat:
    executor: n8n
    webhook_path: jobs/smoke-heartbeat
    timeout_s: 300
    max_attempts: 3
    idempotent: true
    frontend_allowed: false
${overrides}`);
}

export function testConfig(overrides: Partial<Config> = {}): Config {
  return {
    port: 0,
    dbPath: ":memory:",
    registryPath: "unused",
    bearerToken: "test-bearer-token-value",
    webhookSecret: "test-webhook-secret",
    n8nBaseUrl: "http://n8n.test:5678",
    n8nApiKey: null,
    dispatchConcurrency: 2,
    bridgeProbeIntervalMs: 60_000,
    maxBudgetCapUsd: 25,
    nasHost: "truenas-bulk-52tb",
    nasArtifactsBase: "/mnt/BulkPoolZ2/artifacts",
    ...overrides,
  };
}

export function validEnvelope(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    task_type: "smoke-heartbeat",
    payload: {},
    budget_cap: 0,
    artifacts_out: [],
    ...overrides,
  };
}

export class ManualClock {
  constructor(public t = 1_000_000) {}
  now = (): number => this.t;
  advance(ms: number): void {
    this.t += ms;
  }
}

export function makeQueue(db: Database.Database, registry: Registry, clock: ManualClock): Queue {
  return new Queue(db, registry, clock.now);
}
