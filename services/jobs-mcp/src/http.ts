import { timingSafeEqual } from "node:crypto";
import express from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { Queue } from "./queue.js";
import type { Registry } from "./registry.js";
import type { Config } from "./config.js";
import type { Metrics } from "./metrics.js";
import { buildMcpServer } from "./mcp.js";
import { writePing } from "./db.js";
import type Database from "better-sqlite3";

function bearerOk(header: string | undefined, token: string): boolean {
  if (!header || !header.startsWith("Bearer ")) return false;
  const presented = Buffer.from(header.slice(7));
  const expected = Buffer.from(token);
  return presented.length === expected.length && timingSafeEqual(presented, expected);
}

export interface AppOpts {
  registryError?: string | null;
  log?: (line: Record<string, unknown>) => void;
}

export function buildApp(db: Database.Database, queue: Queue, registry: Registry, config: Config, metrics: Metrics, opts: AppOpts = {}) {
  const registryError = opts.registryError ?? null;
  const log = opts.log ?? (() => {});
  const app = express();
  app.use(express.json({ limit: "256kb" }));

  // Liveness: process up + DB open, NEVER a write — a full PVC must not
  // become CrashLoopBackOff (spec §8).
  app.get("/healthz", (_req, res) => {
    try {
      db.prepare("SELECT 1").get();
      res.status(200).json({ ok: true });
    } catch {
      res.status(500).json({ ok: false });
    }
  });

  // Readiness: DB write-ping + registry parsed + token loaded (spec §8).
  // n8n reachability is deliberately absent from both probes.
  app.get("/readyz", (_req, res) => {
    try {
      writePing(db);
      if (registryError !== null) throw new Error(`registry parse failed: ${registryError}`);
      if (registry.size === 0) throw new Error("registry empty");
      if (!config.bearerToken) throw new Error("token missing");
      res.status(200).json({ ok: true });
    } catch (err) {
      res.status(503).json({ ok: false, reason: String(err) });
    }
  });

  app.get("/metrics", async (_req, res) => {
    res.set("content-type", metrics.registry.contentType);
    res.send(await metrics.registry.metrics());
  });

  // MCP Streamable HTTP, stateless mode (spec §2): a fresh transport per
  // request, bearer-gated.
  app.post("/mcp", async (req, res) => {
    if (!bearerOk(req.headers.authorization, config.bearerToken)) {
      res.status(401).json({ code: "E_UNAUTHORIZED", message: "missing or invalid bearer token", retryable: false });
      return;
    }
    const server = buildMcpServer(queue, registry, config, metrics, log);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on("close", () => {
      void transport.close();
      void server.close();
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  });

  return app;
}
