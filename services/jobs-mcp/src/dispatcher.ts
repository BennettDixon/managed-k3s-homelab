import { z } from "zod";
import type { Queue, JobRow } from "./queue.js";
import type { Registry } from "./registry.js";
import { jobArtifactsDir, type Config } from "./config.js";
import type { Metrics } from "./metrics.js";
import { isSafeArtifactPath } from "./envelope.js";

// n8n deterministic-executor bridge (spec §6): one synchronous POST to
// /webhook/jobs/<task_type>, answered by a Respond-to-Webhook final node.
// No callbacks, no execution polling, no stop endpoint (n8n has none).
//
// The completion report is UNTRUSTED input: a misconfigured Respond node can
// return null, a string, or garbage shapes. It is schema-validated before a
// single property is read — reviewer-verified that a blind cast here crashes
// the process via unhandled rejection, and repeats after every boot sweep.
// nullish() everywhere optional: n8n expressions yield explicit JSON nulls
// for absent values, and rejecting those would terminally fail good jobs.
// Manifest entries are pruned to the contract fields (no passthrough) and
// error strings capped — all of this ends up persisted in the row.
const CompletionReportSchema = z.object({
  ok: z.boolean(),
  result: z.unknown(),
  artifacts: z.array(z.object({ name: z.string(), bytes: z.number().nullish(), sha256: z.string().max(128).nullish() })).nullish(),
  spent_usd: z.number().nullish(),
  error: z.object({ code: z.string().max(64).nullish(), message: z.string().max(1024).nullish() }).nullish(),
});

const MAX_RESULT_BYTES = 64 * 1024;
// The whole response body is byte-capped BEFORE parsing: result (≤64KiB) +
// manifest + wrapper must fit; anything bigger is an artifact, and an
// unbounded res.json() would let one bad Respond node OOM the pod.
const MAX_RESPONSE_BYTES = 256 * 1024;
const MAX_MANIFEST_ENTRIES = 64;

// Read a response body with a hard byte cap; null means "over cap".
async function readBodyCapped(res: Response, cap: number): Promise<string | null> {
  const declared = Number(res.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > cap) return null;
  if (!res.body) {
    const text = await res.text();
    return Buffer.byteLength(text, "utf8") > cap ? null : text;
  }
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > cap) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

export interface DispatcherOpts {
  fetchImpl?: typeof fetch;
  log?: (line: Record<string, unknown>) => void;
}

// undici severs a fetch whose response headers haven't arrived with a
// TypeError whose cause is a headers-timeout — NOT an AbortSignal
// TimeoutError. The n8n bridge only sends headers when the workflow
// completes, so this shape means "executor too slow", never "bridge down".
function isTimeoutError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  if (err.name === "TimeoutError" || err.name === "AbortError") return true;
  const cause = (err as { cause?: { code?: string; name?: string } }).cause;
  return cause?.code === "UND_ERR_HEADERS_TIMEOUT" || cause?.name === "HeadersTimeoutError";
}

export class Dispatcher {
  private inFlight = new Set<Promise<void>>();
  private stopping = false;
  private bridgeFailures = 0;
  private bridgeBackoffUntil = 0;
  private timer: NodeJS.Timeout | null = null;
  private fetchImpl: typeof fetch;
  private log: (line: Record<string, unknown>) => void;

  constructor(
    private queue: Queue,
    private registry: Registry,
    private config: Config,
    private metrics: Metrics,
    private now: () => number = Date.now,
    opts: DispatcherOpts = {},
  ) {
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.log = opts.log ?? ((line) => console.log(JSON.stringify(line)));
  }

  start(intervalMs = 500): void {
    this.timer = setInterval(() => this.tick(), intervalMs);
    this.timer.unref();
  }

  // SIGTERM drain (spec §5): stop claiming, then actually WAIT for in-flight
  // webhook awaits up to the grace period — a deploy must not consume an
  // attempt on a job whose execution would have completed in milliseconds.
  async drain(graceMs = 25_000): Promise<void> {
    this.stopping = true;
    if (this.timer) clearInterval(this.timer);
    const pending = Promise.allSettled([...this.inFlight]);
    await Promise.race([pending, new Promise((r) => setTimeout(r, graceMs))]);
  }

  get bridgeUp(): boolean {
    return this.bridgeFailures === 0;
  }

  get inFlightCount(): number {
    return this.inFlight.size;
  }

  // Exception barrier: tick runs inside a bare timer callback, so a throwing
  // DB write (e.g. SQLITE_FULL) must degrade to a logged error, never an
  // uncaughtException crash-loop (spec §10: "PVC full ⇒ ... no crash-loop").
  tick(): void {
    try {
      if (this.stopping) return;
      if (this.now() < this.bridgeBackoffUntil) return;
      while (this.inFlight.size < this.config.dispatchConcurrency) {
        const job = this.queue.claimNext();
        if (!job) break;
        this.metrics.transitions.labels("queued", "running").inc();
        this.log({ evt: "transition", job_id: job.id, task_type: job.task_type, from: "queued", to: "running", attempt: job.attempts });
        const p = this.dispatch(job)
          .catch((err) => {
            // dispatch() has its own barriers; this is the last resort.
            this.log({ evt: "dispatch_unhandled", job_id: job.id, error: String(err) });
            try {
              this.queue.failAttempt(job.id, { code: "internal_dispatch_error", message: String(err), retryable: true });
            } catch {
              /* full-disk worst case: leave the row running for the boot sweep */
            }
          })
          .finally(() => {
            this.inFlight.delete(p);
          });
        this.inFlight.add(p);
      }
    } catch (err) {
      this.log({ evt: "tick_error", error: String(err) });
    }
  }

  async dispatch(job: JobRow): Promise<void> {
    const entry = this.registry.get(job.task_type);
    if (!entry) {
      // Registry changed under a queued job (hash-rolled deploy): terminal.
      this.queue.failTerminal(job.id, { code: "task_type_removed", message: `task_type ${job.task_type} no longer in registry`, retryable: false });
      this.logTransition(job, "failed", 0, "task_type_removed");
      return;
    }
    const started = this.now();
    const body = {
      job_id: job.id,
      task_type: job.task_type,
      attempt: job.attempts,
      payload: JSON.parse(job.payload),
      budget_cap_usd: job.budget_cap_usd,
      artifacts_dir: jobArtifactsDir(this.config, job.task_type, job.id),
      artifacts_out: JSON.parse(job.artifacts_out),
    };

    let res: Response;
    try {
      res = await this.fetchImpl(`${this.config.n8nBaseUrl}/webhook/${entry.webhook_path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-jobs-webhook-secret": this.config.webhookSecret,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(entry.timeout_s * 1000),
      });
    } catch (err: unknown) {
      if (isTimeoutError(err)) {
        // Timeout = abandon locally, attempt failed (spec §5); the execution
        // may still finish in n8n — harmless by the idempotency contract.
        this.failAttempt(job, started, { code: "timeout", message: `no response within ${entry.timeout_s}s`, retryable: true });
      } else {
        // Connection-level failure: the bridge is down. Nothing is failed
        // because of bridge downtime (spec §5) — revert the claim, back off.
        this.bridgeFailures++;
        const backoffMs = Math.min(2_000 * 2 ** Math.min(this.bridgeFailures, 6), 120_000);
        this.bridgeBackoffUntil = this.now() + backoffMs;
        this.metrics.bridgeUp.set(0);
        this.queue.requeueBridgeDown(job.id, this.bridgeBackoffUntil);
        // The revert is a real state transition — it must appear in the
        // transitions counter (or queued→running double-counts during
        // outages) and the per-transition log stream.
        this.metrics.transitions.labels("running", "queued").inc();
        this.log({ evt: "transition", job_id: job.id, task_type: job.task_type, from: "running", to: "queued", attempt: job.attempts - 1, error: "bridge_down" });
        this.log({ evt: "bridge_down", job_id: job.id, backoff_ms: backoffMs, error: String(err) });
      }
      return;
    }

    this.bridgeFailures = 0;
    this.metrics.bridgeUp.set(1);

    if (!res.ok) {
      this.failAttempt(job, started, { code: "executor_http_error", message: `webhook returned ${res.status}`, retryable: true });
      return;
    }

    const bodyText = await readBodyCapped(res, MAX_RESPONSE_BYTES);
    if (bodyText === null) {
      // Deterministic contract violation: the same response comes back on
      // every retry, so this is terminal (retryable: false → failTerminal
      // via queue.failAttempt's non-retryable branch).
      this.failAttempt(job, started, { code: "executor_response_too_large", message: `response exceeds ${MAX_RESPONSE_BYTES} bytes; anything bigger is an artifact`, retryable: false });
      return;
    }
    let rawBody: unknown;
    try {
      rawBody = JSON.parse(bodyText);
    } catch {
      this.failAttempt(job, started, { code: "executor_bad_response", message: "webhook response was not JSON", retryable: true });
      return;
    }
    const parsed = CompletionReportSchema.safeParse(rawBody);
    if (!parsed.success) {
      this.failAttempt(job, started, { code: "executor_bad_response", message: "completion report does not match the bridge contract", retryable: true });
      return;
    }
    const report = parsed.data;

    this.metrics.dispatchDuration.labels(job.task_type).observe((this.now() - started) / 1000);

    if (!report.ok) {
      this.failAttempt(job, started, {
        code: report.error?.code ?? "executor_reported_failure",
        message: report.error?.message ?? "executor reported ok: false",
        retryable: true,
      });
      return;
    }

    const resultJson = report.result === undefined || report.result === null ? null : JSON.stringify(report.result);
    if (resultJson !== null && Buffer.byteLength(resultJson, "utf8") > MAX_RESULT_BYTES) {
      this.failAttempt(job, started, { code: "result_too_large", message: "result exceeds 64 KiB; anything bigger is an artifact", retryable: false });
      return;
    }

    // Artifact contract (spec §7, hard): every declared artifacts_out path must
    // appear in the reported manifest, else the job FAILS (terminal). Reported-
    // but-undeclared files are recorded and flagged, not fatal — but every
    // reported NAME must still pass the same path fence as enqueue: a
    // traversal name from a compromised workflow would otherwise be served
    // verbatim inside artifacts(id) URIs.
    const declared: string[] = JSON.parse(job.artifacts_out);
    const reported = report.artifacts ?? [];
    if (reported.length > MAX_MANIFEST_ENTRIES) {
      this.failAttempt(job, started, { code: "artifacts_invalid", message: `manifest exceeds ${MAX_MANIFEST_ENTRIES} entries`, retryable: false });
      return;
    }
    const unsafe = reported.find((a) => !isSafeArtifactPath(a.name));
    if (unsafe) {
      this.failAttempt(job, started, { code: "artifacts_invalid", message: `reported artifact name invalid or escapes the job directory: ${unsafe.name.slice(0, 200)}`, retryable: false });
      return;
    }
    const reportedNames = new Set(reported.map((a) => a.name));
    const missing = declared.filter((d) => !reportedNames.has(d));
    const manifest = reported.map((a) => ({ name: a.name, bytes: a.bytes ?? undefined, sha256: a.sha256 ?? undefined, undeclared: !declared.includes(a.name) || undefined }));

    if (missing.length > 0) {
      this.queue.failTerminal(
        job.id,
        { code: "artifacts_missing", message: `declared but not reported: ${missing.join(", ")}`, retryable: false },
        JSON.stringify(manifest),
      );
      this.logTransition(job, "failed", this.now() - started, "artifacts_missing");
      return;
    }

    this.queue.succeed(job.id, resultJson, report.spent_usd ?? null, JSON.stringify(manifest));
    this.logTransition(job, "succeeded", this.now() - started);
  }

  private failAttempt(job: JobRow, started: number, error: { code: string; message: string; retryable: boolean }): void {
    this.queue.failAttempt(job.id, error);
    const after = this.queue.get(job.id);
    this.logTransition(job, after?.state ?? "unknown", this.now() - started, error.code);
  }

  private logTransition(job: JobRow, to: string, latencyMs: number, errorCode?: string): void {
    this.metrics.transitions.labels("running", to).inc();
    // One structured line per transition; never payload contents (spec §9).
    this.log({
      evt: "transition",
      job_id: job.id,
      task_type: job.task_type,
      from: "running",
      to,
      attempt: job.attempts,
      latency_ms: latencyMs,
      ...(errorCode ? { error: errorCode } : {}),
    });
  }
}
