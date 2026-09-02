import express from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type Database from "better-sqlite3";
import { readPing } from "./db.js";
import { resolveCaller } from "./auth.js";
import { buildMcpServer } from "./mcp.js";
import type { Config } from "./config.js";
import type { Registry } from "./registry.js";
import type { Store } from "./store.js";
import type { Metrics } from "./metrics.js";

export interface AppOpts {
  registryError?: string | null;
  log?: (line: Record<string, unknown>) => void;
}

export function buildApp(
  db: Database.Database,
  store: Store,
  registry: Registry,
  config: Config,
  metrics: Metrics,
  opts: AppOpts = {},
) {
  const registryError = opts.registryError ?? null;
  const log = opts.log ?? (() => {});
  const app = express();
  // 512kb: explicitly ≥ the largest per-corpus max_doc_bytes (spec §2) — the
  // jobs-mcp 256kb clone would reject legal future push-ingest bodies.
  app.use(express.json({ limit: "512kb" }));

  // Liveness: process up + DB open, NEVER a write (spec §8).
  app.get("/healthz", (_req, res) => {
    try {
      readPing(db);
      res.status(200).json({ ok: true });
    } catch {
      res.status(500).json({ ok: false });
    }
  });

  // Readiness: READ ping + registry parsed + caller tokens loaded. The
  // deliberate deviation from jobs-mcp (spec §8): no write-ping — a full disk
  // degrades ingest via error+metric, and search keeps serving.
  app.get("/readyz", (_req, res) => {
    try {
      readPing(db);
      if (registryError !== null) throw new Error(`registry parse failed: ${registryError}`);
      if (registry.corpora.size === 0) throw new Error("registry has no corpora");
      if (config.callerTokens.size === 0) throw new Error("caller tokens missing");
      res.status(200).json({ ok: true });
    } catch (err) {
      res.status(503).json({ ok: false, reason: String(err) });
    }
  });

  app.get("/metrics", async (_req, res) => {
    // collect() callbacks run SQL per scrape; a throwing collector degrades
    // to a failed scrape, never an unhandled rejection (jobs-mcp precedent).
    try {
      const text = await metrics.registry.metrics();
      res.set("content-type", metrics.registry.contentType);
      res.send(text);
    } catch (err) {
      log({ evt: "metrics_scrape_failed", error: String(err) });
      res.status(500).send("metrics collection failed");
    }
  });

  // Streamable HTTP conformance (jobs-mcp precedent): GET/DELETE /mcp are 405.
  app.get("/mcp", (_req, res) => {
    res.status(405).set("allow", "POST").json({ code: "E_SCHEMA", message: "SSE not offered; use POST", retryable: false });
  });
  app.delete("/mcp", (_req, res) => {
    res.status(405).set("allow", "POST").json({ code: "E_SCHEMA", message: "sessions are stateless; nothing to terminate", retryable: false });
  });

  // MCP Streamable HTTP, stateless (spec §2): fresh transport per request,
  // caller resolved by the token map before anything else.
  app.post("/mcp", async (req, res) => {
    const caller = resolveCaller(req.headers.authorization, config, registry);
    if (!caller) {
      res.status(401).json({ code: "E_UNAUTHORIZED", message: "missing or invalid bearer token", retryable: false });
      return;
    }
    const server = buildMcpServer(store, registry, config, metrics, caller, log);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on("close", () => {
      void transport.close();
      void server.close();
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  });

  app.use((err: Error & { status?: number }, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
    res.status(err.status ?? 400).json({ code: "E_SCHEMA", message: "invalid request body", retryable: false });
  });

  return app;
}
