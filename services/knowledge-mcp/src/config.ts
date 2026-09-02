export interface Config {
  port: number;
  dbPath: string;
  registryPath: string;
  // Caller-token JSON map (spec §2): {"operator": "<token>", ...}. Parsed at
  // boot; the SM secret carries only id→token — policy lives in the registry.
  callerTokens: Map<string, string>;
  fetchTimeoutMs: number;
  searchK: number;
  searchKMax: number;
  fetchInlineCapBytes: number;
}

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

// The token map is validated loudly at boot: a truncated or single-string
// secret must fail the pod, not silently authorize nobody (or worse, map a
// caller id to "undefined").
export function parseCallerTokens(raw: string): Map<string, string> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("KNOWLEDGE_CALLER_TOKENS is not valid JSON");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("KNOWLEDGE_CALLER_TOKENS must be a JSON object of caller_id -> token");
  }
  const map = new Map<string, string>();
  const seen = new Set<string>();
  for (const [id, token] of Object.entries(parsed)) {
    if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(id)) throw new Error(`caller id ${JSON.stringify(id)} is not a valid identifier`);
    if (typeof token !== "string" || token.length < 16) throw new Error(`caller ${id}: token must be a string of at least 16 chars`);
    // A duplicated value would make resolveCaller's full-iteration loop
    // resolve to whichever id sorts last — silent identity confusion from a
    // paste error (review, 2026-09-02).
    if (seen.has(token)) throw new Error(`caller ${id}: token value duplicates another caller's`);
    seen.add(token);
    map.set(id, token);
  }
  if (map.size === 0) throw new Error("KNOWLEDGE_CALLER_TOKENS contains no callers");
  return map;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const required = (name: string): string => {
    const v = env[name];
    if (!v) throw new Error(`missing required env: ${name}`);
    return v;
  };
  return {
    port: intEnv(env, "PORT", 8080, 1, 65535),
    dbPath: env.DB_PATH ?? "/data/knowledge.db",
    registryPath: env.REGISTRY_PATH ?? "/config/corpora.yaml",
    callerTokens: parseCallerTokens(required("KNOWLEDGE_CALLER_TOKENS")),
    fetchTimeoutMs: intEnv(env, "FETCH_TIMEOUT_MS", 10_000, 100, 60_000),
    searchK: intEnv(env, "SEARCH_K_DEFAULT", 6, 1, 25),
    searchKMax: intEnv(env, "SEARCH_K_MAX", 25, 1, 25),
    // Above this, fetch(doc_id) returns metadata + chunk list instead of
    // inline content (spec §3 — MCP results land verbatim in agent context).
    fetchInlineCapBytes: intEnv(env, "FETCH_INLINE_CAP_BYTES", 65_536, 1_024, 1_048_576),
  };
}
