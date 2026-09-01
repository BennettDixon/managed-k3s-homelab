import { describe, it, expect } from "vitest";
import { loadConfig, jobArtifactsDir } from "../src/config.js";

const baseEnv = { JOBS_BEARER_TOKEN: "t", JOBS_WEBHOOK_SECRET: "s" };

describe("config validation (env coercion must fail loudly)", () => {
  it("defaults are applied when vars are unset", () => {
    const c = loadConfig(baseEnv);
    expect(c.dispatchConcurrency).toBe(2);
    expect(c.maxBudgetCapUsd).toBe(25);
    expect(c.port).toBe(8080);
  });

  it("empty numeric env vars throw instead of coercing to 0", () => {
    expect(() => loadConfig({ ...baseEnv, DISPATCH_CONCURRENCY: "" })).toThrowError(/DISPATCH_CONCURRENCY/);
  });

  it("malformed numbers throw instead of coercing to NaN", () => {
    expect(() => loadConfig({ ...baseEnv, MAX_BUDGET_CAP_USD: "25 USD" })).toThrowError(/MAX_BUDGET_CAP_USD/);
    expect(() => loadConfig({ ...baseEnv, PORT: "eighty" })).toThrowError(/PORT/);
    expect(() => loadConfig({ ...baseEnv, DISPATCH_CONCURRENCY: "2.5" })).toThrowError(/DISPATCH_CONCURRENCY/);
  });

  it("out-of-range values throw", () => {
    expect(() => loadConfig({ ...baseEnv, DISPATCH_CONCURRENCY: "0" })).toThrowError();
    expect(() => loadConfig({ ...baseEnv, PORT: "70000" })).toThrowError();
  });

  it("trailing slashes on N8N_BASE_URL are normalized away", () => {
    const c = loadConfig({ ...baseEnv, N8N_BASE_URL: "http://n8n.test:5678///" });
    expect(c.n8nBaseUrl).toBe("http://n8n.test:5678");
  });

  it("jobArtifactsDir is the single source of the per-job path", () => {
    const c = loadConfig(baseEnv);
    expect(jobArtifactsDir(c, "smoke-heartbeat", "01X")).toBe("/mnt/BulkPoolZ2/artifacts/jobs/smoke-heartbeat/01X/");
  });
});
