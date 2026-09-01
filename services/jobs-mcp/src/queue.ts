import type Database from "better-sqlite3";
import { monotonicFactory } from "ulid";
import { JobsError } from "./errors.js";
import { envelopeHash, type Envelope } from "./envelope.js";
import type { Registry } from "./registry.js";

// Job lifecycle (spec §5): queued → running → succeeded | failed | canceled.
// Every transition is one SQLite transaction. Terminal rows are immutable.
export type JobState = "queued" | "running" | "succeeded" | "failed" | "canceled";

export interface JobRow {
  id: string;
  task_type: string;
  payload: string;
  budget_cap_usd: number;
  priority: number;
  artifacts_out: string;
  idempotency_key: string | null;
  envelope_hash: string | null;
  state: JobState;
  attempts: number;
  max_attempts: number;
  not_before: number | null;
  error: string | null;
  result: string | null;
  spent_usd: number | null;
  artifacts: string | null;
  enqueued_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface EnqueueResult {
  id: string;
  idempotent_replay: boolean;
}

const ulid = monotonicFactory();

export class Queue {
  constructor(
    private db: Database.Database,
    private registry: Registry,
    private now: () => number = Date.now,
  ) {}

  enqueue(envelope: Envelope): EnqueueResult {
    const entry = this.registry.get(envelope.task_type);
    if (!entry) throw new JobsError("E_TASK_TYPE_UNKNOWN", `task_type not in registry: ${envelope.task_type}`);
    const hash = envelopeHash(envelope);

    const tx = this.db.transaction((): EnqueueResult => {
      if (envelope.idempotency_key) {
        const existing = this.db
          .prepare("SELECT id, envelope_hash FROM jobs WHERE idempotency_key = ?")
          .get(envelope.idempotency_key) as Pick<JobRow, "id" | "envelope_hash"> | undefined;
        if (existing) {
          if (existing.envelope_hash === hash) return { id: existing.id, idempotent_replay: true };
          throw new JobsError("E_CONFLICT_IDEMPOTENCY", "idempotency_key reused with a different envelope");
        }
      }
      const id = ulid(this.now());
      this.db
        .prepare(
          `INSERT INTO jobs (id, task_type, payload, budget_cap_usd, priority, artifacts_out,
             idempotency_key, envelope_hash, state, attempts, max_attempts, enqueued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)`,
        )
        .run(
          id,
          envelope.task_type,
          JSON.stringify(envelope.payload),
          envelope.budget_cap,
          envelope.priority,
          JSON.stringify(envelope.artifacts_out),
          envelope.idempotency_key ?? null,
          hash,
          entry.max_attempts,
          this.now(),
        );
      return { id, idempotent_replay: false };
    });
    return tx();
  }

  get(id: string): JobRow | undefined {
    return this.db.prepare("SELECT * FROM jobs WHERE id = ?").get(id) as JobRow | undefined;
  }

  // Claim the next dispatchable job: strictly (priority ASC, id ASC), ULIDs
  // give FIFO within a priority band (spec §3). Commits `running` BEFORE the
  // HTTP call happens — intent durable before side effect (spec §5).
  claimNext(): JobRow | undefined {
    const tx = this.db.transaction((): JobRow | undefined => {
      const row = this.db
        .prepare(
          `SELECT * FROM jobs WHERE state = 'queued' AND (not_before IS NULL OR not_before <= ?)
           ORDER BY priority ASC, id ASC LIMIT 1`,
        )
        .get(this.now()) as JobRow | undefined;
      if (!row) return undefined;
      this.db
        .prepare("UPDATE jobs SET state = 'running', attempts = attempts + 1, started_at = ? WHERE id = ?")
        .run(this.now(), row.id);
      return this.get(row.id);
    });
    return tx();
  }

  succeed(id: string, result: string | null, spentUsd: number | null, artifactsManifest: string): void {
    this.db
      .prepare(
        "UPDATE jobs SET state = 'succeeded', result = ?, spent_usd = ?, artifacts = ?, finished_at = ? WHERE id = ? AND state = 'running'",
      )
      .run(result, spentUsd, artifactsManifest, this.now(), id);
  }

  failTerminal(id: string, error: { code: string; message: string; retryable: boolean }, artifactsManifest?: string): void {
    this.db
      .prepare(
        "UPDATE jobs SET state = 'failed', error = ?, artifacts = COALESCE(?, artifacts), finished_at = ? WHERE id = ? AND state = 'running'",
      )
      .run(JSON.stringify(error), artifactsManifest ?? null, this.now(), id);
  }

  // Jittered exponential backoff (spec §5): 30s·2^attempts, cap 15 min.
  private nextAttemptAt(attempts: number): number {
    const backoffMs = Math.min(30_000 * 2 ** attempts, 15 * 60_000);
    return this.now() + backoffMs + Math.floor(backoffMs * 0.2 * Math.random());
  }

  // Attempt failed: retry with backoff, or fail terminally when attempts are
  // exhausted OR the error is non-retryable — a deterministic contract
  // violation (result_too_large, artifacts_invalid) re-runs identically, so
  // retrying it only wastes executions and buries the real error code.
  failAttempt(id: string, error: { code: string; message: string; retryable: boolean }): void {
    const tx = this.db.transaction(() => {
      const row = this.get(id);
      if (!row || row.state !== "running") return;
      if (!error.retryable || row.attempts >= row.max_attempts) {
        const terminal = error.retryable ? { code: "retries_exhausted", message: error.message, retryable: false } : error;
        this.db
          .prepare("UPDATE jobs SET state = 'failed', error = ?, finished_at = ? WHERE id = ?")
          .run(JSON.stringify(terminal), this.now(), id);
      } else {
        this.db
          .prepare("UPDATE jobs SET state = 'queued', error = ?, not_before = ?, started_at = NULL WHERE id = ?")
          .run(JSON.stringify(error), this.nextAttemptAt(row.attempts), id);
      }
    });
    tx();
  }

  // Bridge-down requeue (spec §5): n8n unreachable fails NOTHING — the claim
  // is reverted (attempt un-consumed) and the job waits out the bridge backoff.
  requeueBridgeDown(id: string, notBefore: number): void {
    this.db
      .prepare("UPDATE jobs SET state = 'queued', attempts = attempts - 1, started_at = NULL, not_before = ? WHERE id = ? AND state = 'running'")
      .run(notBefore, id);
  }

  cancel(id: string): JobRow {
    const tx = this.db.transaction((): JobRow => {
      const row = this.get(id);
      if (!row) throw new JobsError("E_NOT_FOUND", `no such job: ${id}`);
      if (row.state !== "queued") {
        throw new JobsError("E_NOT_CANCELABLE", `job is ${row.state}; only queued jobs can be canceled (stopping a running n8n execution is not offered)`);
      }
      this.db.prepare("UPDATE jobs SET state = 'canceled', finished_at = ? WHERE id = ?").run(this.now(), id);
      return this.get(id)!;
    });
    return tx();
  }

  // Boot sweep (spec §5): with one process and one writer, any `running` row
  // found at boot is by definition an orphan. No leases, no heartbeats.
  bootSweep(): { requeued: number; failed: number } {
    const tx = this.db.transaction(() => {
      const orphans = this.db.prepare("SELECT * FROM jobs WHERE state = 'running'").all() as JobRow[];
      let requeued = 0;
      let failed = 0;
      for (const row of orphans) {
        if (row.attempts < row.max_attempts) {
          this.db
            .prepare("UPDATE jobs SET state = 'queued', not_before = ?, started_at = NULL WHERE id = ?")
            .run(this.nextAttemptAt(row.attempts), row.id);
          requeued++;
        } else {
          this.db
            .prepare("UPDATE jobs SET state = 'failed', error = ?, finished_at = ? WHERE id = ?")
            .run(
              JSON.stringify({
                code: "retries_exhausted",
                message: "orphaned at boot with no attempts left; the last execution may still have completed in n8n (harmless by idempotency)",
                retryable: false,
              }),
              this.now(),
              row.id,
            );
          failed++;
        }
      }
      return { requeued, failed };
    });
    return tx();
  }

  stateCounts(): { state: string; task_type: string; n: number }[] {
    return this.db
      .prepare("SELECT state, task_type, COUNT(*) AS n FROM jobs GROUP BY state, task_type")
      .all() as { state: string; task_type: string; n: number }[];
  }

  oldestQueuedAgeMs(): number {
    const row = this.db.prepare("SELECT MIN(enqueued_at) AS t FROM jobs WHERE state = 'queued'").get() as { t: number | null };
    return row.t === null ? 0 : this.now() - row.t;
  }
}
