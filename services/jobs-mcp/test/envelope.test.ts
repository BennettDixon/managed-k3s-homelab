import { describe, it, expect } from "vitest";
import { validateEnvelope, envelopeHash } from "../src/envelope.js";
import { JobsError } from "../src/errors.js";
import { testRegistry, validEnvelope } from "./helpers.js";

const registry = testRegistry();
const validate = (raw: unknown) => validateEnvelope(raw, registry, 25);
const codeOf = (raw: unknown): string => {
  try {
    validate(raw);
    return "OK";
  } catch (e) {
    return (e as JobsError).code;
  }
};

describe("envelope validation (spec §3)", () => {
  it("accepts a minimal valid envelope and defaults v=1, priority=5", () => {
    const e = validate(validEnvelope());
    expect(e.v).toBe(1);
    expect(e.priority).toBe(5);
  });

  it("rejects unknown top-level fields", () => {
    expect(codeOf(validEnvelope({ extra: 1 }))).toBe("E_SCHEMA");
  });

  it("rejects wrong envelope version", () => {
    expect(codeOf(validEnvelope({ v: 2 }))).toBe("E_ENVELOPE_VERSION");
  });

  it("rejects unknown task_type", () => {
    expect(codeOf(validEnvelope({ task_type: "nope" }))).toBe("E_TASK_TYPE_UNKNOWN");
  });

  it("budget_cap: missing and null are E_BUDGET_CAP_MISSING — never defaulted", () => {
    const noCap = validEnvelope();
    delete (noCap as Record<string, unknown>).budget_cap;
    expect(codeOf(noCap)).toBe("E_BUDGET_CAP_MISSING");
    expect(codeOf(validEnvelope({ budget_cap: null }))).toBe("E_BUDGET_CAP_MISSING");
  });

  it("budget_cap: 0 is legal (no model spend permitted)", () => {
    expect(validate(validEnvelope({ budget_cap: 0 })).budget_cap).toBe(0);
  });

  it("budget_cap: negative, NaN, string, and over-cap are E_BUDGET_CAP_INVALID", () => {
    expect(codeOf(validEnvelope({ budget_cap: -1 }))).toBe("E_BUDGET_CAP_INVALID");
    expect(codeOf(validEnvelope({ budget_cap: Number.NaN }))).toBe("E_BUDGET_CAP_INVALID");
    expect(codeOf(validEnvelope({ budget_cap: "5" }))).toBe("E_BUDGET_CAP_INVALID");
    expect(codeOf(validEnvelope({ budget_cap: 25.01 }))).toBe("E_BUDGET_CAP_INVALID");
  });

  it("priority bounds are enforced", () => {
    expect(codeOf(validEnvelope({ priority: -1 }))).toBe("E_SCHEMA");
    expect(codeOf(validEnvelope({ priority: 10 }))).toBe("E_SCHEMA");
    expect(codeOf(validEnvelope({ priority: 2.5 }))).toBe("E_SCHEMA");
    expect(validate(validEnvelope({ priority: 0 })).priority).toBe(0);
  });

  it("artifacts_out: traversal and absolute paths are rejected", () => {
    expect(codeOf(validEnvelope({ artifacts_out: ["../escape.txt"] }))).toBe("E_SCHEMA");
    expect(codeOf(validEnvelope({ artifacts_out: ["a/../../b"] }))).toBe("E_SCHEMA");
    expect(codeOf(validEnvelope({ artifacts_out: ["/abs.txt"] }))).toBe("E_SCHEMA");
    expect(codeOf(validEnvelope({ artifacts_out: Array.from({ length: 33 }, (_, i) => `f${i}`) }))).toBe("E_SCHEMA");
    expect(validate(validEnvelope({ artifacts_out: ["out/report.json"] })).artifacts_out).toEqual(["out/report.json"]);
  });

  it("payload over 64 KiB is E_PAYLOAD_INVALID", () => {
    expect(codeOf(validEnvelope({ payload: { blob: "x".repeat(65 * 1024) } }))).toBe("E_PAYLOAD_INVALID");
  });

  it("idempotency_key length bounds", () => {
    expect(codeOf(validEnvelope({ idempotency_key: "short" }))).toBe("E_SCHEMA");
    expect(codeOf(validEnvelope({ idempotency_key: "x".repeat(129) }))).toBe("E_SCHEMA");
  });

  it("payload_schema from the registry is enforced (E_PAYLOAD_INVALID)", () => {
    const strict = testRegistry(`  schema-task:
    executor: n8n
    webhook_path: jobs/schema-task
    timeout_s: 60
    max_attempts: 1
    idempotent: true
    payload_schema:
      type: object
      additionalProperties: false
      properties:
        name: { type: string }
      required: [name]
`);
    const v = (raw: unknown) => validateEnvelope(raw, strict, 25);
    expect(v(validEnvelope({ task_type: "schema-task", payload: { name: "x" } })).task_type).toBe("schema-task");
    expect(() => v(validEnvelope({ task_type: "schema-task", payload: { junk: 1 } }))).toThrowError(
      expect.objectContaining({ code: "E_PAYLOAD_INVALID" }),
    );
    expect(() => v(validEnvelope({ task_type: "schema-task", payload: {} }))).toThrowError(
      expect.objectContaining({ code: "E_PAYLOAD_INVALID" }),
    );
  });

  it("envelope hash is stable under payload key order", () => {
    const a = validate(validEnvelope({ payload: { x: 1, y: { b: 2, a: 3 } } }));
    const b = validate(validEnvelope({ payload: { y: { a: 3, b: 2 }, x: 1 } }));
    expect(envelopeHash(a)).toBe(envelopeHash(b));
  });
});
