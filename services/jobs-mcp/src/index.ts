import { statSync } from "node:fs";
import { loadConfig } from "./config.js";
import { openDb } from "./db.js";
import { loadRegistry, type Registry } from "./registry.js";
import { Queue } from "./queue.js";
import { Metrics } from "./metrics.js";
import { Dispatcher } from "./dispatcher.js";
import { buildApp } from "./http.js";
import { n8nStartupCheck } from "./n8n-check.js";

const log = (line: Record<string, unknown>) => console.log(JSON.stringify({ ts: new Date().toISOString(), ...line }));

const config = loadConfig();
const db = openDb(config.dbPath);

// Registry parse failure fails READINESS, not the process (spec §4): a bad
// registry PR must yield an alive-but-NotReady pod, never CrashLoopBackOff.
// With a failed parse nothing dispatches — existing queued jobs must NOT be
// terminal-failed as "task_type_removed" by an empty map.
let registry: Registry = new Map();
let registryError: string | null = null;
try {
  registry = loadRegistry(config.registryPath);
} catch (err) {
  registryError = String(err);
  log({ evt: "registry_parse_failed", error: registryError });
}

const queue = new Queue(db, registry);

// Boot sweep before anything dispatches (spec §5).
const swept = queue.bootSweep();
log({ evt: "boot_sweep", ...swept });

const metrics = new Metrics(queue, () => {
  try {
    return statSync(config.dbPath).size;
  } catch {
    return 0;
  }
});
metrics.transitions.labels("running", "queued").inc(swept.requeued);
metrics.transitions.labels("running", "failed").inc(swept.failed);

const dispatcher = new Dispatcher(queue, registry, config, metrics, Date.now, { log });
if (registryError === null) {
  dispatcher.start();
  void n8nStartupCheck(config, registry, log);
}

const app = buildApp(db, queue, registry, config, metrics, { registryError, log });
const server = app.listen(config.port, () => log({ evt: "listening", port: config.port, task_types: [...registry.keys()], degraded: registryError !== null }));

// SIGTERM (spec §5): stop claiming, WAIT for in-flight webhook awaits within
// the grace period (terminationGracePeriodSeconds is 30s; we drain for 25s),
// then exit. The boot sweep recovers anything still severed.
process.on("SIGTERM", () => {
  log({ evt: "sigterm", in_flight: dispatcher.inFlightCount });
  void dispatcher.drain(25_000).then(() => {
    server.close(() => {
      db.close();
      process.exit(0);
    });
    // Idle keep-alive connections must not block exit past the grace period.
    setTimeout(() => process.exit(0), 3_000).unref();
  });
});
