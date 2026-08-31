import { Registry as PromRegistry, Gauge, Counter, Histogram } from "prom-client";
import type { Queue } from "./queue.js";

// Metrics (spec §9). The ServiceMonitor label requirement lives in slice 2;
// this side just serves /metrics.
export class Metrics {
  readonly registry = new PromRegistry();
  readonly bridgeUp: Gauge;
  readonly transitions: Counter;
  readonly enqueued: Counter;
  readonly dispatchDuration: Histogram;

  constructor(queue: Queue, dbBytes: () => number) {
    this.bridgeUp = new Gauge({ name: "jobs_bridge_up", help: "1 if the last n8n webhook call succeeded at the connection level", registers: [this.registry] });
    this.bridgeUp.set(1);
    this.transitions = new Counter({ name: "jobs_transitions_total", help: "job state transitions", labelNames: ["from", "to"], registers: [this.registry] });
    this.enqueued = new Counter({ name: "jobs_enqueued_total", help: "jobs enqueued", labelNames: ["task_type"], registers: [this.registry] });
    this.dispatchDuration = new Histogram({
      name: "jobs_dispatch_duration_seconds",
      help: "webhook round-trip duration",
      labelNames: ["task_type"],
      buckets: [0.1, 0.5, 1, 5, 15, 60, 300],
      registers: [this.registry],
    });
    new Gauge({
      name: "jobs_state_count",
      help: "jobs per state",
      labelNames: ["state"],
      registers: [this.registry],
      collect() {
        const counts = queue.stateCounts();
        for (const s of ["queued", "running", "succeeded", "failed", "canceled"]) this.labels(s).set(counts[s] ?? 0);
      },
    });
    new Gauge({
      name: "jobs_queue_oldest_age_seconds",
      help: "age of the oldest queued job",
      registers: [this.registry],
      collect() {
        this.set(queue.oldestQueuedAgeMs() / 1000);
      },
    });
    new Gauge({
      name: "jobs_db_bytes",
      help: "size of the SQLite database file",
      registers: [this.registry],
      collect() {
        this.set(dbBytes());
      },
    });
  }
}
