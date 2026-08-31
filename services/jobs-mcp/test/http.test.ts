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

  it("mcp endpoint accepts the right bearer token end-to-end (initialize + tool call)", async () => {
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

    const init = await call({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "test", version: "0" } },
    });
    expect(init.status).toBe(200);

    const toolCall = await call({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        name: "enqueue",
        arguments: { task_type: "smoke-heartbeat", payload: {}, budget_cap: 0, artifacts_out: [] },
      },
    });
    expect(toolCall.status).toBe(200);
    const text = await toolCall.text();
    expect(text).toContain("idempotent_replay");
  });
});
