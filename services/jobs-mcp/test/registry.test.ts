import { describe, it, expect } from "vitest";
import { parseRegistry } from "../src/registry.js";

describe("registry admission (spec §4)", () => {
  it("parses a valid registry", () => {
    const r = parseRegistry(`
task_types:
  smoke-heartbeat:
    executor: n8n
    webhook_path: jobs/smoke-heartbeat
    timeout_s: 300
    max_attempts: 3
    idempotent: true
`);
    expect(r.get("smoke-heartbeat")!.frontend_allowed).toBe(false);
  });

  it("rejects executor n8n without idempotent: true", () => {
    expect(() =>
      parseRegistry(`
task_types:
  bad-task:
    executor: n8n
    webhook_path: jobs/bad-task
    timeout_s: 60
    max_attempts: 1
    idempotent: false
`),
    ).toThrowError(/idempotent/);
  });

  it("rejects unknown executors (worker is reserved for v2)", () => {
    expect(() =>
      parseRegistry(`
task_types:
  future-task:
    executor: worker
    webhook_path: jobs/future-task
    timeout_s: 60
    max_attempts: 1
    idempotent: true
`),
    ).toThrowError();
  });

  it("rejects timeout_s above 300 (undici severs headers at 300s regardless)", () => {
    expect(() =>
      parseRegistry(`
task_types:
  slow-task:
    executor: n8n
    webhook_path: jobs/slow-task
    timeout_s: 301
    max_attempts: 1
    idempotent: true
`),
    ).toThrowError();
  });

  it("rejects a TYPO'D schema keyword (ajv strict): additionalproperties must not silently no-op", () => {
    expect(() =>
      parseRegistry(`
task_types:
  typo-schema:
    executor: n8n
    webhook_path: jobs/typo-schema
    timeout_s: 60
    max_attempts: 1
    idempotent: true
    payload_schema:
      type: object
      additionalproperties: false
`),
    ).toThrowError();
  });

  it("compiles payload_schema at parse time and rejects a broken schema", () => {
    expect(() =>
      parseRegistry(`
task_types:
  bad-schema:
    executor: n8n
    webhook_path: jobs/bad-schema
    timeout_s: 60
    max_attempts: 1
    idempotent: true
    payload_schema:
      type: not-a-real-type
`),
    ).toThrowError();
  });

  it("rejects invalid task_type names", () => {
    expect(() =>
      parseRegistry(`
task_types:
  Bad_Name:
    executor: n8n
    webhook_path: jobs/x
    timeout_s: 60
    max_attempts: 1
    idempotent: true
`),
    ).toThrowError();
  });
});
