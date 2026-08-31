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

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const required = (name: string): string => {
    const v = env[name];
    if (!v) throw new Error(`missing required env: ${name}`);
    return v;
  };
  return {
    port: Number(env.PORT ?? 8080),
    dbPath: env.DB_PATH ?? "/data/jobs.db",
    registryPath: env.REGISTRY_PATH ?? "/config/registry.yaml",
    bearerToken: required("JOBS_BEARER_TOKEN"),
    webhookSecret: required("JOBS_WEBHOOK_SECRET"),
    n8nBaseUrl: env.N8N_BASE_URL ?? "http://n8n:5678",
    n8nApiKey: env.N8N_API_KEY ?? null,
    dispatchConcurrency: Number(env.DISPATCH_CONCURRENCY ?? 2),
    maxBudgetCapUsd: Number(env.MAX_BUDGET_CAP_USD ?? 25),
    nasHost: env.NAS_HOST ?? "truenas-bulk-52tb",
    nasArtifactsBase: env.NAS_ARTIFACTS_BASE ?? "/mnt/BulkPoolZ2/artifacts",
  };
}
