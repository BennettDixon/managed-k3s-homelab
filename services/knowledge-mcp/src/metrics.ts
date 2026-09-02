import { Registry as PromRegistry, Counter, Gauge, Histogram } from "prom-client";
import type { Store } from "./store.js";
import type { Registry } from "./registry.js";

// Metrics per spec §9. Collect callbacks run SQL per scrape; http.ts wraps
// scraping so a throwing collector degrades to a failed scrape, never a
// crashed process.
export class Metrics {
  registry = new PromRegistry();
  searchTotal: Counter;
  searchDuration: Histogram;
  ingestTotal: Counter;
  ingestErrors: Counter;

  constructor(store: Store, corpora: Registry["corpora"], dbBytes: () => number, now: () => number = Date.now) {
    this.searchTotal = new Counter({
      name: "knowledge_search_total",
      help: "search calls",
      labelNames: ["corpus"],
      registers: [this.registry],
    });
    this.searchDuration = new Histogram({
      name: "knowledge_search_duration_seconds",
      help: "search latency",
      buckets: [0.001, 0.005, 0.02, 0.1, 0.5, 2],
      registers: [this.registry],
    });
    this.ingestTotal = new Counter({
      name: "knowledge_ingest_total",
      help: "ingest results",
      labelNames: ["corpus", "result"],
      registers: [this.registry],
    });
    this.ingestErrors = new Counter({
      name: "knowledge_ingest_errors_total",
      help: "ingest failures",
      labelNames: ["corpus", "code"],
      registers: [this.registry],
    });
    const corporaNames = [...corpora.keys()];
    new Gauge({
      name: "knowledge_docs",
      help: "live documents",
      labelNames: ["corpus"],
      registers: [this.registry],
      collect() {
        for (const c of corporaNames) this.labels(c).set(store.counts(c).docs);
      },
    });
    new Gauge({
      name: "knowledge_chunks",
      help: "live chunks",
      labelNames: ["corpus"],
      registers: [this.registry],
      collect() {
        for (const c of corporaNames) this.labels(c).set(store.counts(c).chunks);
      },
    });
    new Gauge({
      name: "knowledge_db_bytes",
      help: "size of knowledge.db",
      registers: [this.registry],
      collect() {
        this.set(dbBytes());
      },
    });
    new Gauge({
      name: "knowledge_index_age_seconds",
      help: "seconds since last successful reingest per corpus",
      labelNames: ["corpus"],
      registers: [this.registry],
      collect() {
        for (const c of corporaNames) {
          const meta = store.corpusMeta(c);
          if (meta.ingested_at !== null) this.labels(c).set(Math.max(0, (now() - meta.ingested_at) / 1000));
        }
      },
    });
  }
}
