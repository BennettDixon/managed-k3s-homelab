import { statSync } from "node:fs";
import { loadConfig } from "./config.js";
import { openDb } from "./db.js";
import { loadRegistry, type Registry } from "./registry.js";
import { Store } from "./store.js";
import { Metrics } from "./metrics.js";
import { buildApp } from "./http.js";

const log = (line: Record<string, unknown>) => console.log(JSON.stringify({ ts: new Date().toISOString(), ...line }));

const config = loadConfig();
const db = openDb(config.dbPath);

// Registry parse failure fails READINESS, not the process (spec §4): a bad
// registry PR yields an alive-but-NotReady pod, never CrashLoopBackOff.
let registry: Registry = { corpora: new Map(), callers: new Map() };
let registryError: string | null = null;
try {
  registry = loadRegistry(config.registryPath);
} catch (err) {
  registryError = String(err);
  log({ evt: "registry_parse_failed", error: registryError });
}

const store = new Store(db);
// Chunker reconciliation before serving (spec §5): a chunker-version bump
// re-chunks every live doc from stored content, so FTS rows, chunk ids, and
// neighbors always agree with the running code.
store.rechunkIfStale(log);
const metrics = new Metrics(store, registry.corpora, () => {
  try {
    return statSync(config.dbPath).size;
  } catch {
    return 0;
  }
});

const app = buildApp(db, store, registry, config, metrics, { registryError, log });
const server = app.listen(config.port, () =>
  log({
    evt: "listening",
    port: config.port,
    corpora: [...registry.corpora.keys()],
    callers: [...registry.callers.keys()],
    degraded: registryError !== null,
  }),
);

// SIGTERM: no dispatcher to drain here (serving is request-scoped); stop
// accepting, let in-flight requests finish, close the DB, exit within the
// grace period. Per-doc transactions make any severed ingest harmless.
process.on("SIGTERM", () => {
  log({ evt: "sigterm" });
  server.close(() => {
    db.close();
    process.exit(0);
  });
  setTimeout(() => process.exit(0), 5_000).unref();
});
