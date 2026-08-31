import type { Config } from "./config.js";
import type { Registry } from "./registry.js";

// Non-fatal startup check (spec §6): verify each registry workflow exists and
// is active via the n8n API. n8n being down must NOT block enqueue — this
// logs and moves on. The API key's use is deliberately minimal (blast radius:
// community n8n keys are full-access).
export async function n8nStartupCheck(
  config: Config,
  registry: Registry,
  log: (line: Record<string, unknown>) => void,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  if (!config.n8nApiKey) {
    log({ evt: "n8n_check_skipped", reason: "no N8N_API_KEY configured" });
    return;
  }
  try {
    const res = await fetchImpl(`${config.n8nBaseUrl}/api/v1/workflows?active=true`, {
      headers: { "X-N8N-API-KEY": config.n8nApiKey },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      log({ evt: "n8n_check_failed", status: res.status });
      return;
    }
    const body = (await res.json()) as { data?: { name?: string }[] };
    const activeNames = new Set((body.data ?? []).map((w) => w.name ?? ""));
    for (const [taskType] of registry) {
      // Convention: the workflow is named after its task_type.
      if (!activeNames.has(taskType)) {
        log({ evt: "n8n_check_workflow_missing", task_type: taskType, note: "no active workflow with this name" });
      }
    }
  } catch (err) {
    log({ evt: "n8n_check_unreachable", error: String(err) });
  }
}
