import { describe, it, expect, beforeEach, vi } from "vitest";
import type Database from "better-sqlite3";
import { testDb, testRegistry, testConfig, validEnvelope, ManualClock, makeQueue } from "./helpers.js";
import { validateEnvelope } from "../src/envelope.js";
import { Dispatcher } from "../src/dispatcher.js";
import { Metrics } from "../src/metrics.js";
import type { Queue } from "../src/queue.js";

const registry = testRegistry();
const config = testConfig();
let db: Database.Database;
let clock: ManualClock;
let queue: Queue;
let metrics: Metrics;

const env = (overrides: Record<string, unknown> = {}) => validateEnvelope(validEnvelope(overrides), registry, 25);
const silent = () => {};

function fakeFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response> | never): typeof fetch {
  return (async (url: unknown, init: unknown) => handler(String(url), init as RequestInit)) as typeof fetch;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  db = testDb();
  clock = new ManualClock();
  queue = makeQueue(db, registry, clock);
  metrics = new Metrics(queue, () => 0);
});

function makeDispatcher(fetchImpl: typeof fetch): Dispatcher {
  return new Dispatcher(queue, registry, config, metrics, clock.now, { fetchImpl, log: silent });
}

describe("dispatcher / n8n bridge (spec §5–§7)", () => {
  it("success path: posts the full contract body with the secret header, records result + spent + manifest", async () => {
    let captured: { url: string; headers: Record<string, string>; body: Record<string, unknown> } | null = null;
    const d = makeDispatcher(
      fakeFetch((url, init) => {
        captured = { url, headers: init.headers as Record<string, string>, body: JSON.parse(String(init.body)) };
        return jsonResponse({ ok: true, result: { done: 1 }, artifacts: [], spent_usd: 0 });
      }),
    );
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);

    expect(captured!.url).toBe("http://n8n.test:5678/webhook/jobs/smoke-heartbeat");
    expect(captured!.headers["x-jobs-webhook-secret"]).toBe("test-webhook-secret");
    expect(captured!.body).toMatchObject({
      job_id: id,
      task_type: "smoke-heartbeat",
      attempt: 1,
      budget_cap_usd: 0,
      artifacts_dir: `/mnt/BulkPoolZ2/artifacts/jobs/smoke-heartbeat/${id}/`,
      artifacts_out: [],
    });
    const row = queue.get(id)!;
    expect(row.state).toBe("succeeded");
    expect(JSON.parse(row.result!)).toEqual({ done: 1 });
    expect(row.spent_usd).toBe(0);
  });

  it("ok:false is an attempt failure that retries, then exhausts to terminal", async () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse({ ok: false, error: { code: "boom", message: "exec failed" } })));
    const { id } = queue.enqueue(env());
    for (let i = 0; i < 3; i++) {
      clock.advance(20 * 60_000);
      const claimed = queue.claimNext();
      expect(claimed).toBeDefined();
      await d.dispatch(claimed!);
    }
    const row = queue.get(id)!;
    expect(row.state).toBe("failed");
    expect(JSON.parse(row.error!).code).toBe("retries_exhausted");
  });

  it("non-2xx is an attempt failure", async () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse({}, 500)));
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("queued");
    expect(JSON.parse(row.error!).code).toBe("executor_http_error");
  });

  it("timeout abandons locally as an attempt failure (no stop endpoint exists)", async () => {
    const d = makeDispatcher(
      fakeFetch(() => {
        const err = new Error("aborted");
        err.name = "TimeoutError";
        throw err;
      }),
    );
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("queued");
    expect(JSON.parse(row.error!).code).toBe("timeout");
    expect(row.attempts).toBe(1); // timeout consumes the attempt
  });

  it("connection failure = bridge down: claim reverted, no attempt consumed, nothing failed", async () => {
    const d = makeDispatcher(
      fakeFetch(() => {
        throw new TypeError("fetch failed: ECONNREFUSED");
      }),
    );
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("queued");
    expect(row.attempts).toBe(0);
    expect(row.error).toBeNull();
    expect(d.bridgeUp).toBe(false);
  });

  it("artifacts contract: declared-but-unreported fails the job terminally with artifacts_missing", async () => {
    const d = makeDispatcher(
      fakeFetch(() => jsonResponse({ ok: true, artifacts: [{ name: "extra.log", bytes: 10 }] })),
    );
    const { id } = queue.enqueue(env({ artifacts_out: ["report.json"] }));
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("failed");
    expect(JSON.parse(row.error!).code).toBe("artifacts_missing");
    // Undeclared extras are recorded and flagged, not fatal in themselves.
    const manifest = JSON.parse(row.artifacts!);
    expect(manifest[0]).toMatchObject({ name: "extra.log", undeclared: true });
  });

  it("declared artifacts reported = success; undeclared extras flagged in the manifest", async () => {
    const d = makeDispatcher(
      fakeFetch(() =>
        jsonResponse({ ok: true, artifacts: [{ name: "report.json", bytes: 42, sha256: "abc" }, { name: "debug.log" }] }),
      ),
    );
    const { id } = queue.enqueue(env({ artifacts_out: ["report.json"] }));
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("succeeded");
    const manifest = JSON.parse(row.artifacts!) as { name: string; undeclared?: boolean }[];
    expect(manifest.find((a) => a.name === "report.json")!.undeclared).toBeUndefined();
    expect(manifest.find((a) => a.name === "debug.log")!.undeclared).toBe(true);
  });

  it("oversized result is TERMINAL with its own code — deterministic violations are not retried", async () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse({ ok: true, result: { blob: "x".repeat(65 * 1024) } })));
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("failed");
    expect(JSON.parse(row.error!).code).toBe("result_too_large");
  });

  it("oversized response BODY is capped before parsing and fails terminally", async () => {
    const d = makeDispatcher(fakeFetch(() => new Response("x", { status: 200, headers: { "content-length": "999999999" } })));
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("failed");
    expect(JSON.parse(row.error!).code).toBe("executor_response_too_large");
  });

  it("explicit nulls in the completion report are tolerated (n8n expressions yield null)", async () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse({ ok: true, result: null, artifacts: null, spent_usd: null })));
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("succeeded");
    expect(row.result).toBeNull();
  });

  it("a traversal artifact name from the executor fails the job terminally, never served", async () => {
    const d = makeDispatcher(
      fakeFetch(() => jsonResponse({ ok: true, artifacts: [{ name: "report.json" }, { name: "../../../../secrets/tailnet.key" }] })),
    );
    const { id } = queue.enqueue(env({ artifacts_out: ["report.json"] }));
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.state).toBe("failed");
    expect(JSON.parse(row.error!).code).toBe("artifacts_invalid");
    expect(row.artifacts).toBeNull(); // nothing unsafe was persisted
  });

  it("a manifest over the entry cap fails terminally", async () => {
    const many = Array.from({ length: 65 }, (_, i) => ({ name: `f${i}.txt` }));
    const d = makeDispatcher(fakeFetch(() => jsonResponse({ ok: true, artifacts: many })));
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    expect(JSON.parse(queue.get(id)!.error!).code).toBe("artifacts_invalid");
  });

  it("bridge-down revert emits the running->queued transition (counter balance)", async () => {
    const lines: Record<string, unknown>[] = [];
    const d = new Dispatcher(queue, registry, config, metrics, clock.now, {
      fetchImpl: fakeFetch(() => {
        throw new TypeError("fetch failed: ECONNREFUSED");
      }),
      log: (l) => lines.push(l),
    });
    queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    expect(lines.some((l) => l.evt === "transition" && l.from === "running" && l.to === "queued")).toBe(true);
  });

  it("a null completion body is an attempt failure, never a crash (reviewer-verified crash bug)", async () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse(null)));
    const { id } = queue.enqueue(env());
    await expect(d.dispatch(queue.claimNext()!)).resolves.toBeUndefined();
    expect(JSON.parse(queue.get(id)!.error!).code).toBe("executor_bad_response");
  });

  it("a non-array artifacts field is an attempt failure, never a crash", async () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse({ ok: true, artifacts: "nope" })));
    const { id } = queue.enqueue(env());
    await expect(d.dispatch(queue.claimNext()!)).resolves.toBeUndefined();
    expect(JSON.parse(queue.get(id)!.error!).code).toBe("executor_bad_response");
  });

  it("undici headers-timeout (TypeError + UND_ERR_HEADERS_TIMEOUT cause) is a TIMEOUT, not bridge-down", async () => {
    const d = makeDispatcher(
      fakeFetch(() => {
        const err = new TypeError("fetch failed");
        (err as Error & { cause?: unknown }).cause = { code: "UND_ERR_HEADERS_TIMEOUT", name: "HeadersTimeoutError" };
        throw err;
      }),
    );
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    const row = queue.get(id)!;
    expect(row.attempts).toBe(1); // attempt consumed — no infinite re-trigger loop
    expect(JSON.parse(row.error!).code).toBe("timeout");
    expect(d.bridgeUp).toBe(true);
  });

  it("drain waits for in-flight dispatches instead of severing them", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((r) => (release = r));
    const d = makeDispatcher(fakeFetch(() => gate));
    const { id } = queue.enqueue(env());
    const inFlight = d.dispatch(queue.claimNext()!);
    const drained = d.drain(5_000);
    release(jsonResponse({ ok: true, result: { late: 1 } }));
    await inFlight;
    await drained;
    expect(queue.get(id)!.state).toBe("succeeded");
  });

  it("tick survives a throwing claim (full-disk shape) without escaping the timer callback", () => {
    const d = makeDispatcher(fakeFetch(() => jsonResponse({ ok: true })));
    const broken = Object.create(queue) as typeof queue;
    broken.claimNext = () => {
      throw new Error("SQLITE_FULL: database or disk is full");
    };
    // @ts-expect-error reaching into the private field to simulate the failure
    d.queue = broken;
    expect(() => d.tick()).not.toThrow();
  });

  it("task_type removed from registry under a queued job fails terminally", async () => {
    const shrunk = testRegistry();
    shrunk.delete("smoke-heartbeat");
    const d = new Dispatcher(queue, shrunk, config, metrics, clock.now, { fetchImpl: fakeFetch(() => jsonResponse({ ok: true })), log: silent });
    const { id } = queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    expect(queue.get(id)!.state).toBe("failed");
    expect(JSON.parse(queue.get(id)!.error!).code).toBe("task_type_removed");
  });
});

describe("dispatcher / idle bridge probe (spec §9)", () => {
  it("a thrown connection error marks the bridge down", async () => {
    const d = makeDispatcher(
      fakeFetch(() => {
        throw new TypeError("fetch failed: ECONNREFUSED");
      }),
    );
    expect(d.bridgeUp).toBe(true); // starts up
    await d.probe();
    expect(d.bridgeUp).toBe(false);
  });

  it("marks up on ANY response (404 included), via GET to the base URL — never a webhook path", async () => {
    let throwIt = true;
    const seen: { url: string; method?: string }[] = [];
    const d = makeDispatcher(
      fakeFetch((url, init) => {
        seen.push({ url, method: init.method });
        if (throwIt) throw new TypeError("fetch failed: ECONNREFUSED");
        return new Response("not found", { status: 404 });
      }),
    );
    await d.probe(); // connection error → down
    expect(d.bridgeUp).toBe(false);
    throwIt = false;
    clock.advance(200_000); // clear the backoff the down-probe armed (cap 120s)
    await d.probe(); // a 404 still proves the connection came up ⇒ up
    expect(d.bridgeUp).toBe(true);
    expect(seen.length).toBe(2);
    for (const c of seen) {
      expect(c.method).toBe("GET");
      expect(c.url).toBe(config.n8nBaseUrl);
      expect(c.url).not.toContain("/webhook"); // MUST NOT be able to fire a workflow
    }
  });

  it("honors bridgeBackoffUntil and reuses the backoff a real dispatch armed", async () => {
    let calls = 0;
    const d = makeDispatcher(
      fakeFetch(() => {
        calls++;
        throw new TypeError("fetch failed: ECONNREFUSED");
      }),
    );
    // Arm the backoff via a real dispatch connection failure.
    queue.enqueue(env());
    await d.dispatch(queue.claimNext()!);
    expect(d.bridgeUp).toBe(false);
    const afterDispatch = calls; // 1 (the webhook attempt)
    // Within the backoff window the probe must NOT fetch.
    await d.probe();
    expect(calls).toBe(afterDispatch);
    // Once the window expires the probe checks again (idle recovery detection).
    clock.advance(200_000);
    await d.probe();
    expect(calls).toBe(afterDispatch + 1);
  });

  it("yields to a tick-driven dispatch already in flight — no gauge clobber", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((r) => (release = r));
    let baseProbes = 0;
    const d = makeDispatcher(
      fakeFetch((url) => {
        if (url === config.n8nBaseUrl) {
          baseProbes++;
          return new Response("x", { status: 200 });
        }
        return gate; // the webhook dispatch hangs, holding a slot in flight
      }),
    );
    queue.enqueue(env());
    d.tick(); // claims + dispatches → inFlight === 1, awaiting the gate
    expect(d.inFlightCount).toBe(1);
    await d.probe(); // must skip: a real dispatch is in flight
    expect(baseProbes).toBe(0);
    release(jsonResponse({ ok: true, result: { ok: 1 } }));
    await d.drain(5_000); // let the dispatch settle
    expect(d.bridgeUp).toBe(true);
  });

  it("tick refuses to claim while a probe is in flight (probing guard)", async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((r) => (release = r));
    let webhookCalls = 0;
    const d = makeDispatcher(
      fakeFetch((url) => {
        if (url === config.n8nBaseUrl) return gate; // probe's connection check hangs
        webhookCalls++;
        return jsonResponse({ ok: true, result: {} });
      }),
    );
    const { id } = queue.enqueue(env());
    const probing = d.probe(); // starts, sets probing=true, awaits the gate
    d.tick(); // must NOT claim while a probe is in flight
    expect(webhookCalls).toBe(0);
    expect(queue.get(id)!.state).toBe("queued");
    release(new Response("ok", { status: 200 }));
    await probing;
    expect(d.bridgeUp).toBe(true);
  });

  it("BRIDGE_PROBE_INTERVAL_MS=0 disables the probe: start() schedules none", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const cfg = testConfig({ bridgeProbeIntervalMs: 0 });
      const d = new Dispatcher(queue, registry, cfg, metrics, clock.now, {
        fetchImpl: fakeFetch(() => {
          calls++;
          return jsonResponse({ ok: true });
        }),
        log: silent,
      });
      d.start();
      await vi.advanceTimersByTimeAsync(5 * 60_000); // 5 min — many probe intervals
      await d.drain(0);
      expect(calls).toBe(0); // no probe fired; empty queue means tick fetches nothing either
    } finally {
      vi.useRealTimers();
    }
  });

  it("when enabled and idle, the probe fires on its interval, GETting only the base URL", async () => {
    vi.useFakeTimers();
    try {
      const seen: { url: string; method?: string }[] = [];
      const cfg = testConfig({ bridgeProbeIntervalMs: 60_000 });
      const d = new Dispatcher(queue, registry, cfg, metrics, clock.now, {
        fetchImpl: fakeFetch((url, init) => {
          seen.push({ url, method: init.method });
          return new Response("ok", { status: 200 });
        }),
        log: silent,
      });
      d.start();
      await vi.advanceTimersByTimeAsync(60_000); // one probe interval
      await d.drain(0);
      expect(seen.length).toBeGreaterThanOrEqual(1);
      for (const c of seen) {
        expect(c.method).toBe("GET");
        expect(c.url).toBe(cfg.n8nBaseUrl);
        expect(c.url).not.toContain("/webhook");
      }
    } finally {
      vi.useRealTimers();
    }
  });
});
