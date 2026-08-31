import { describe, it, expect, beforeEach } from "vitest";
import type Database from "better-sqlite3";
import { testDb, testRegistry, validEnvelope, ManualClock, makeQueue } from "./helpers.js";
import { validateEnvelope } from "../src/envelope.js";
import { JobsError } from "../src/errors.js";
import type { Queue } from "../src/queue.js";

const registry = testRegistry();
let db: Database.Database;
let clock: ManualClock;
let queue: Queue;

const env = (overrides: Record<string, unknown> = {}) => validateEnvelope(validEnvelope(overrides), registry, 25);

beforeEach(() => {
  db = testDb();
  clock = new ManualClock();
  queue = makeQueue(db, registry, clock);
});

describe("queue (spec §5)", () => {
  it("enqueue persists a queued row with the registry's max_attempts", () => {
    const { id, idempotent_replay } = queue.enqueue(env());
    const row = queue.get(id)!;
    expect(idempotent_replay).toBe(false);
    expect(row.state).toBe("queued");
    expect(row.max_attempts).toBe(3);
    expect(row.budget_cap_usd).toBe(0);
  });

  it("idempotency: same key + same envelope replays; different envelope conflicts", () => {
    const first = queue.enqueue(env({ idempotency_key: "client-key-1" }));
    const replay = queue.enqueue(env({ idempotency_key: "client-key-1" }));
    expect(replay.id).toBe(first.id);
    expect(replay.idempotent_replay).toBe(true);

    expect(() => queue.enqueue(env({ idempotency_key: "client-key-1", budget_cap: 1 }))).toThrowError(
      expect.objectContaining({ code: "E_CONFLICT_IDEMPOTENCY" }),
    );
  });

  it("claim order is strictly (priority ASC, id ASC) — FIFO within a band", () => {
    const low = queue.enqueue(env({ priority: 7 }));
    clock.advance(10);
    const urgentA = queue.enqueue(env({ priority: 1 }));
    clock.advance(10);
    const urgentB = queue.enqueue(env({ priority: 1 }));
    expect(queue.claimNext()!.id).toBe(urgentA.id);
    expect(queue.claimNext()!.id).toBe(urgentB.id);
    expect(queue.claimNext()!.id).toBe(low.id);
  });

  it("claim commits running + attempts+1 (durable intent before side effect)", () => {
    const { id } = queue.enqueue(env());
    const claimed = queue.claimNext()!;
    expect(claimed.id).toBe(id);
    expect(claimed.state).toBe("running");
    expect(claimed.attempts).toBe(1);
    expect(queue.claimNext()).toBeUndefined();
  });

  it("not_before gates dispatch until the backoff elapses", () => {
    const { id } = queue.enqueue(env());
    queue.claimNext();
    queue.failAttempt(id, { code: "timeout", message: "t", retryable: true });
    expect(queue.get(id)!.state).toBe("queued");
    expect(queue.claimNext()).toBeUndefined(); // still backing off
    clock.advance(16 * 60_000);
    expect(queue.claimNext()!.id).toBe(id);
  });

  it("attempts exhaust into terminal failed {retries_exhausted}", () => {
    const { id } = queue.enqueue(env());
    for (let i = 0; i < 3; i++) {
      clock.advance(20 * 60_000);
      const claimed = queue.claimNext();
      expect(claimed?.id).toBe(id);
      queue.failAttempt(id, { code: "timeout", message: "t", retryable: true });
    }
    const row = queue.get(id)!;
    expect(row.state).toBe("failed");
    expect(JSON.parse(row.error!).code).toBe("retries_exhausted");
    expect(row.finished_at).not.toBeNull();
  });

  it("bridge-down requeue reverts the claim without consuming an attempt", () => {
    const { id } = queue.enqueue(env());
    queue.claimNext();
    queue.requeueBridgeDown(id, clock.now() + 5_000);
    const row = queue.get(id)!;
    expect(row.state).toBe("queued");
    expect(row.attempts).toBe(0);
    expect(row.started_at).toBeNull();
  });

  it("cancel: queued only; running and terminal states refuse", () => {
    const a = queue.enqueue(env());
    expect(queue.cancel(a.id).state).toBe("canceled");

    const b = queue.enqueue(env());
    queue.claimNext();
    expect(() => queue.cancel(b.id)).toThrowError(expect.objectContaining({ code: "E_NOT_CANCELABLE" }));
    expect(() => queue.cancel("01JUNKID000000000000000000")).toThrowError(expect.objectContaining({ code: "E_NOT_FOUND" }));
  });

  it("boot sweep: orphaned running rows requeue with attempts left, fail terminal otherwise", () => {
    const fresh = queue.enqueue(env());
    queue.claimNext(); // attempts=1 of 3 → requeue

    const spent = queue.enqueue(env());
    db.prepare("UPDATE jobs SET state = 'running', attempts = 3 WHERE id = ?").run(spent.id); // exhausted orphan

    const swept = queue.bootSweep();
    expect(swept).toEqual({ requeued: 1, failed: 1 });
    expect(queue.get(fresh.id)!.state).toBe("queued");
    const failedRow = queue.get(spent.id)!;
    expect(failedRow.state).toBe("failed");
    expect(JSON.parse(failedRow.error!).code).toBe("retries_exhausted");
  });

  it("terminal rows are immutable through the transition helpers", () => {
    const { id } = queue.enqueue(env());
    queue.claimNext();
    queue.succeed(id, JSON.stringify({ ok: 1 }), 0, "[]");
    queue.failTerminal(id, { code: "x", message: "x", retryable: false });
    expect(queue.get(id)!.state).toBe("succeeded");
  });
});
