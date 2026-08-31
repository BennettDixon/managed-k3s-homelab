import { describe, it, expect, beforeEach, afterEach } from "vitest";
import type { Server } from "node:http";
import { testDb, testRegistry, testConfig, ManualClock, makeQueue } from "./helpers.js";
import { buildApp } from "../src/http.js";
import { Metrics } from "../src/metrics.js";

const config = testConfig();
let server: Server;
let base: string;

beforeEach(async () => {
  const db = testDb();
  const registry = testRegistry();
  const queue = makeQueue(db, registry, new ManualClock());
  const metrics = new Metrics(queue, () => 0);
  const app = buildApp(db, queue, registry, config, metrics);
  await new Promise<void>((resolve) => {
    server = app.listen(0, resolve);
  });
  const addr = server.address();
  base = `http://127.0.0.1:${typeof addr === "object" && addr ? addr.port : 0}`;
});

afterEach(() => {
  server.close();
});

describe("http surface (spec §2, §8)", () => {
  it("healthz and readyz respond ok", async () => {
    expect((await fetch(`${base}/healthz`)).status).toBe(200);
    expect((await fetch(`${base}/readyz`)).status).toBe(200);
  });

  it("metrics exposes the spec'd series", async () => {
    const text = await (await fetch(`${base}/metrics`)).text();
    for (const name of ["jobs_bridge_up", "jobs_state_count", "jobs_queue_oldest_age_seconds", "jobs_db_bytes"]) {
      expect(text).toContain(name);
    }
  });

  it("mcp endpoint rejects a missing or wrong bearer token", async () => {
    const noAuth = await fetch(`${base}/mcp`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    expect(noAuth.status).toBe(401);
    const wrong = await fetch(`${base}/mcp`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer wrong-token" },
      body: "{}",
    });
    expect(wrong.status).toBe(401);
  });

  const call = async (body: unknown) =>
    fetch(`${base}/mcp`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        authorization: `Bearer ${config.bearerToken}`,
      },
      body: JSON.stringify(body),
    });

  const toolCall = async (id: number, name: string, args: Record<string, unknown>) => {
    const res = await call({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });
    expect(res.status).toBe(200);
    const text = await res.text();
    // Extract the tool's JSON payload from the SSE/JSON-RPC wrapper.
    const match = text.match(/"text":"((?:[^"\\]|\\.)*)"/);
    return match ? (JSON.parse(JSON.parse(`"${match[1]}"`)) as Record<string, unknown>) : null;
  };

  it("mcp endpoint accepts the right bearer token end-to-end (initialize + tool call)", async () => {
    const init = await call({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "test", version: "0" } },
    });
    expect(init.status).toBe(200);

    const result = await toolCall(2, "enqueue", { task_type: "smoke-heartbeat", payload: {}, budget_cap: 0, artifacts_out: [] });
    expect(result).toMatchObject({ idempotent_replay: false });
  });

  it("unknown envelope fields reach validateEnvelope and are rejected E_SCHEMA (SDK must not strip them)", async () => {
    const result = await toolCall(3, "enqueue", {
      task_type: "smoke-heartbeat",
      payload: {},
      budget_cap: 0,
      artifacts_out: [],
      idempotancy_key: "typo-key-12345",
    });
    expect(result).toMatchObject({ code: "E_SCHEMA" });
  });

  it("missing budget_cap yields the taxonomy code E_BUDGET_CAP_MISSING through the real surface, not -32602", async () => {
    const result = await toolCall(4, "enqueue", { task_type: "smoke-heartbeat", payload: {}, artifacts_out: [] });
    expect(result).toMatchObject({ code: "E_BUDGET_CAP_MISSING" });
  });

  it("status returns the full fixed shape (§5) and E_NOT_FOUND for unknown ids", async () => {
    const enq = await toolCall(5, "enqueue", { task_type: "smoke-heartbeat", payload: {}, budget_cap: 0, artifacts_out: [] });
    const status = await toolCall(6, "status", { id: enq!.id });
    expect(status).toMatchObject({ id: enq!.id, task_type: "smoke-heartbeat", state: "queued", spent_usd: null, started_at: null, error: null, artifact_count: null });
    const missing = await toolCall(7, "status", { id: "01JNOSUCHJOB0000000000000" });
    expect(missing).toMatchObject({ code: "E_NOT_FOUND" });
  });

  it("artifacts returns scp-style NAS URIs (§7)", async () => {
    const enq = await toolCall(8, "enqueue", { task_type: "smoke-heartbeat", payload: {}, budget_cap: 0, artifacts_out: [] });
    const artifacts = await toolCall(9, "artifacts", { id: enq!.id });
    expect(artifacts).toMatchObject({
      id: enq!.id,
      state: "queued",
      artifacts_dir: `/mnt/BulkPoolZ2/artifacts/jobs/smoke-heartbeat/${enq!.id}/`,
      artifacts: [],
    });
  });
});

describe("readiness on registry failure (spec §4)", () => {
  it("a registry parse failure degrades to 503 NotReady, with healthz still 200", async () => {
    const db = testDb();
    const queue = makeQueue(db, testRegistry(), new ManualClock());
    const metrics = new Metrics(queue, () => 0);
    const app = buildApp(db, queue, new Map(), config, metrics, { registryError: "bad yaml" });
    const srv = await new Promise<Server>((resolve) => {
      const s = app.listen(0, () => resolve(s));
    });
    const addr = srv.address();
    const b = `http://127.0.0.1:${typeof addr === "object" && addr ? addr.port : 0}`;
    expect((await fetch(`${b}/healthz`)).status).toBe(200);
    const ready = await fetch(`${b}/readyz`);
    expect(ready.status).toBe(503);
    expect(await ready.text()).toContain("registry parse failed");
    srv.close();
  });
});
