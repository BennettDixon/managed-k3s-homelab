import Database from "better-sqlite3";
import { openDb } from "../src/db.js";
import { parseRegistry, type Registry, type Caller } from "../src/registry.js";
import { Store, sha256 } from "../src/store.js";
import { Metrics } from "../src/metrics.js";
import { parseCallerTokens, type Config } from "../src/config.js";
import type { Fetcher } from "../src/ingest.js";

export const RAW_PREFIX = "https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/";

export const REGISTRY_YAML = `
corpora:
  homelab-notes:
    description: "repo docs/ + proxmox/ operational notes"
    rebuild_source: git
    visibility: operator
    trust: operator-authored
    allowed_uri_prefixes:
      - ${RAW_PREFIX}docs/
      - ${RAW_PREFIX}proxmox/
    tree_source: https://api.github.com/repos/BennettDixon/managed-k3s-homelab/git/trees/main
    max_doc_bytes: 262144
  public-faq:
    description: "curated frontend-visible corpus"
    rebuild_source: git
    visibility: frontend
    trust: curated
    allowed_uri_prefixes:
      - ${RAW_PREFIX}faq/
callers:
  operator: { class: operator }
  n8n-reingest: { class: reingest-bot, corpora: [homelab-notes] }
  nanoclaw: { class: frontend }
`;

export function testRegistry(): Registry {
  return parseRegistry(REGISTRY_YAML);
}

export function testConfig(overrides: Partial<Config> = {}): Config {
  return {
    port: 0,
    dbPath: ":memory:",
    registryPath: "unused",
    callerTokens: parseCallerTokens(
      JSON.stringify({
        operator: "operator-token-0123456789",
        "n8n-reingest": "reingest-token-0123456789",
        nanoclaw: "nanoclaw-token-0123456789",
      }),
    ),
    fetchTimeoutMs: 1000,
    searchK: 6,
    searchKMax: 25,
    fetchInlineCapBytes: 65_536,
    ...overrides,
  };
}

export function testStore(): { db: Database.Database; store: Store } {
  const db = openDb(":memory:");
  return { db, store: new Store(db) };
}

export function testMetrics(store: Store, registry: Registry): Metrics {
  return new Metrics(store, registry.corpora, () => 0);
}

export function caller(registry: Registry, id: string): Caller {
  const c = registry.callers.get(id);
  if (!c) throw new Error(`no test caller ${id}`);
  return c;
}

// A canned fetcher keyed by exact URL; anything unknown 404s.
export function cannedFetcher(routes: Record<string, string | { status: number; text: string }>): Fetcher {
  return async (url) => {
    const hit = routes[url];
    if (hit === undefined) return { status: 404, text: "not found" };
    return typeof hit === "string" ? { status: 200, text: hit } : hit;
  };
}

export function seedDoc(store: Store, docId: string, corpus: string, uri: string, content: string, trust = "operator-authored"): void {
  store.upsertDoc({
    doc_id: docId,
    corpus,
    uri,
    title: null,
    content,
    content_sha256: sha256(content),
    bytes: Buffer.byteLength(content, "utf8"),
    source_commit: "testsha",
    trust,
    ingested_by: "operator",
    ingested_at: 1000,
  });
}
