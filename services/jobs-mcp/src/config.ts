export interface Config {
  port: number;
  dbPath: string;
  registryPath: string;
  bearerToken: string;
  webhookSecret: string;
  n8nBaseUrl: string;
  n8nApiKey: string | null;
  dispatchConcurrency: number;
  maxBudgetCapUsd: number;
  nasHost: string;
  nasArtifactsBase: string;
}

// Env numbers are validated loudly at boot: a bare Number() coercion turns a
// templating slip ("" or "25 USD") into NaN/0 that silently freezes dispatch
// or disables the budget-cap guard.
function intEnv(env: NodeJS.ProcessEnv, name: string, def: number, min: number, max: number): number {
  const raw = env[name];
  if (raw === undefined || raw === "") {
    if (raw === "") throw new Error(`env ${name} is set but empty`);
    return def;
  }
  const n = Number(raw);
  if (!Number.isInteger(n) || n < min || n > max) throw new Error(`env ${name}=${raw} must be an integer in [${min}, ${max}]`);
  return n;
}

function numEnv(env: NodeJS.ProcessEnv, name: string, def: number, min: number, max: number): number {
  const raw = env[name];
  if (raw === undefined || raw === "") {
    if (raw === "") throw new Error(`env ${name} is set but empty`);
    return def;
  }
  const n = Number(raw);
  if (!Number.isFinite(n) || n < min || n > max) throw new Error(`env ${name}=${raw} must be a finite number in [${min}, ${max}]`);
  return n;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const required = (name: string): string => {
    const v = env[name];
    if (!v) throw new Error(`missing required env: ${name}`);
    return v;
  };
  return {
    port: intEnv(env, "PORT", 8080, 1, 65535),
    dbPath: env.DB_PATH ?? "/data/jobs.db",
    registryPath: env.REGISTRY_PATH ?? "/config/registry.yaml",
    bearerToken: required("JOBS_BEARER_TOKEN"),
    webhookSecret: required("JOBS_WEBHOOK_SECRET"),
    // Trailing slashes are normalized away: `${base}/webhook/...` with a
    // trailing slash yields //webhook, which n8n's router 404s.
    n8nBaseUrl: (env.N8N_BASE_URL ?? "http://n8n:5678").replace(/\/+$/, ""),
    n8nApiKey: env.N8N_API_KEY ?? null,
    dispatchConcurrency: intEnv(env, "DISPATCH_CONCURRENCY", 2, 1, 16),
    maxBudgetCapUsd: numEnv(env, "MAX_BUDGET_CAP_USD", 25, 0.01, 10_000),
    nasHost: env.NAS_HOST ?? "truenas-bulk-52tb",
    nasArtifactsBase: env.NAS_ARTIFACTS_BASE ?? "/mnt/BulkPoolZ2/artifacts",
  };
}

// Single source of truth for the per-job artifact directory — the same string
// is part of the executor contract (dispatcher) and the artifacts(id) reply.
export function jobArtifactsDir(config: Config, taskType: string, jobId: string): string {
  return `${config.nasArtifactsBase}/jobs/${taskType}/${jobId}/`;
}
