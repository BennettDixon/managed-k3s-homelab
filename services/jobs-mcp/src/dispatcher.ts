import type { Queue, JobRow } from "./queue.js";
import type { Registry } from "./registry.js";
import type { Config } from "./config.js";
import type { Metrics } from "./metrics.js";

// n8n deterministic-executor bridge (spec §6): one synchronous POST to
// /webhook/jobs/<task_type>, answered by a Respond-to-Webhook final node.
// No callbacks, no execution polling, no stop endpoint (n8n has none).
interface CompletionReport {
  ok: boolean;
  result?: unknown;
  artifacts?: { name: string; bytes?: number; sha256?: string }[];
  spent_usd?: number;
  error?: { code?: string; message?: string };
}

const MAX_RESULT_BYTES = 64 * 1024;

export interface DispatcherOpts {
  fetchImpl?: typeof fetch;
  log?: (line: Record<string, unknown>) => void;
}

export class Dispatcher {
  private inFlight = 0;
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

  // SIGTERM (spec §5): stop claiming; in-flight awaits either finish within the
  // grace period or are severed and recovered by the next boot sweep.
  stop(): void {
    this.stopping = true;
    if (this.timer) clearInterval(this.timer);
  }

  get bridgeUp(): boolean {
    return this.bridgeFailures === 0;
  }

  tick(): void {
    if (this.stopping) return;
    if (this.now() < this.bridgeBackoffUntil) return;
    while (this.inFlight < this.config.dispatchConcurrency) {
      const job = this.queue.claimNext();
      if (!job) break;
      this.inFlight++;
      void this.dispatch(job).finally(() => {
        this.inFlight--;
      });
    }
  }

  private artifactsDir(job: JobRow): string {
    return `${this.config.nasArtifactsBase}/jobs/${job.task_type}/${job.id}/`;
  }

  async dispatch(job: JobRow): Promise<void> {
    const entry = this.registry.get(job.task_type);
    if (!entry) {
      // Registry changed under a queued job (hash-rolled deploy): terminal.
      this.queue.failTerminal(job.id, { code: "task_type_removed", message: `task_type ${job.task_type} no longer in registry`, retryable: false });
      return;
    }
    const started = this.now();
    const body = {
      job_id: job.id,
      task_type: job.task_type,
      attempt: job.attempts,
      payload: JSON.parse(job.payload),
      budget_cap_usd: job.budget_cap_usd,
      artifacts_dir: this.artifactsDir(job),
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
      if (err instanceof Error && err.name === "TimeoutError") {
        // Timeout = abandon locally, attempt failed (spec §5); the execution
        // may still finish in n8n — harmless by the idempotency contract.
        this.transition(job, "attempt-failed", { code: "timeout", message: `no response within ${entry.timeout_s}s`, retryable: true });
      } else {
        // Connection-level failure: the bridge is down. Nothing is failed
        // because of bridge downtime (spec §5) — revert the claim, back off.
        this.bridgeFailures++;
        const backoffMs = Math.min(2_000 * 2 ** Math.min(this.bridgeFailures, 6), 120_000);
        this.bridgeBackoffUntil = this.now() + backoffMs;
        this.metrics.bridgeUp.set(0);
        this.queue.requeueBridgeDown(job.id, this.bridgeBackoffUntil);
        this.log({ evt: "bridge_down", job_id: job.id, backoff_ms: backoffMs, error: String(err) });
      }
      return;
    }

    this.bridgeFailures = 0;
    this.metrics.bridgeUp.set(1);

    if (!res.ok) {
      this.transition(job, "attempt-failed", { code: "executor_http_error", message: `webhook returned ${res.status}`, retryable: true });
      return;
    }

    let report: CompletionReport;
    try {
      report = (await res.json()) as CompletionReport;
    } catch {
      this.transition(job, "attempt-failed", { code: "executor_bad_response", message: "webhook response was not JSON", retryable: true });
      return;
    }

    this.metrics.dispatchDuration.labels(job.task_type).observe((this.now() - started) / 1000);

    if (report.ok !== true) {
      this.transition(job, "attempt-failed", {
        code: report.error?.code ?? "executor_reported_failure",
        message: report.error?.message ?? "executor reported ok: false",
        retryable: true,
      });
      return;
    }

    const resultJson = report.result === undefined ? null : JSON.stringify(report.result);
    if (resultJson !== null && Buffer.byteLength(resultJson, "utf8") > MAX_RESULT_BYTES) {
      this.transition(job, "attempt-failed", { code: "result_too_large", message: "result exceeds 64 KiB; anything bigger is an artifact", retryable: false });
      return;
    }

    // Artifact contract (spec §7, hard): every declared artifacts_out path must
    // appear in the reported manifest, else the job FAILS (terminal). Reported-
    // but-undeclared files are recorded and flagged, not fatal.
    const declared: string[] = JSON.parse(job.artifacts_out);
    const reported = report.artifacts ?? [];
    const reportedNames = new Set(reported.map((a) => a.name));
    const missing = declared.filter((d) => !reportedNames.has(d));
    const manifest = reported.map((a) => ({ ...a, undeclared: !declared.includes(a.name) || undefined }));

    if (missing.length > 0) {
      this.queue.failTerminal(
        job.id,
        { code: "artifacts_missing", message: `declared but not reported: ${missing.join(", ")}`, retryable: false },
        JSON.stringify(manifest),
      );
      this.logTransition(job, "failed");
      return;
    }

    this.queue.succeed(job.id, resultJson, report.spent_usd ?? null, JSON.stringify(manifest));
    this.logTransition(job, "succeeded");
  }

  private transition(job: JobRow, kind: "attempt-failed", error: { code: string; message: string; retryable: boolean }): void {
    this.queue.failAttempt(job.id, error);
    const after = this.queue.get(job.id);
    this.logTransition(job, after?.state ?? "unknown", error.code);
  }

  private logTransition(job: JobRow, to: string, errorCode?: string): void {
    this.metrics.transitions.labels("running", to).inc();
    // One structured line per transition; never payload contents (spec §9).
    this.log({ evt: "transition", job_id: job.id, task_type: job.task_type, from: "running", to, attempt: job.attempts, ...(errorCode ? { error: errorCode } : {}) });
  }
}
