import { statSync } from "node:fs";
import { loadConfig } from "./config.js";
import { openDb } from "./db.js";
import { loadRegistry } from "./registry.js";
import { Queue } from "./queue.js";
import { Metrics } from "./metrics.js";
import { Dispatcher } from "./dispatcher.js";
import { buildApp } from "./http.js";
import { n8nStartupCheck } from "./n8n-check.js";

const log = (line: Record<string, unknown>) => console.log(JSON.stringify({ ts: new Date().toISOString(), ...line }));

const config = loadConfig();
const db = openDb(config.dbPath);
const registry = loadRegistry(config.registryPath);
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

const dispatcher = new Dispatcher(queue, registry, config, metrics, Date.now, { log });
dispatcher.start();

void n8nStartupCheck(config, registry, log);

const app = buildApp(db, queue, registry, config, metrics);
const server = app.listen(config.port, () => log({ evt: "listening", port: config.port, task_types: [...registry.keys()] }));

// SIGTERM (spec §5): stop claiming, let in-flight finish within the grace
// period, exit; the boot sweep recovers anything severed.
process.on("SIGTERM", () => {
  log({ evt: "sigterm" });
  dispatcher.stop();
  server.close(() => {
    db.close();
    process.exit(0);
  });
});
