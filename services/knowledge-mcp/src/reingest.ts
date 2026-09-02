import { KnowledgeError } from "./errors.js";
import type { Config } from "./config.js";
import type { Corpus } from "./registry.js";
import type { Store } from "./store.js";
import { ingestUri, type Fetcher, defaultFetcher } from "./ingest.js";

// reingest(corpus) — spec §6 freshness: walk the corpus tree source, ingest
// every matching doc (sha short-circuit makes unchanged docs free), tombstone
// docs that vanished from the tree, record the source ref. This is the ONLY
// call the scheduled executor makes; it holds no content and no git.

interface TreeEntry {
  path: string;
  type: string;
}

export interface ReingestResult {
  corpus: string;
  source_ref: string | null;
  indexed: number;
  unchanged: number;
  tombstoned: number;
  errors: Array<{ uri: string; code: string }>;
}

// Map the corpus's allowed raw prefixes to repo-relative path prefixes so the
// tree listing can be filtered: .../main/docs/ -> "docs/".
function pathPrefixes(corpus: Corpus): string[] {
  return corpus.allowed_uri_prefixes.map((p) => {
    const u = new URL(p);
    return u.pathname.split("/").filter(Boolean).slice(3).join("/") + "/";
  });
}

function rawUriFor(corpus: Corpus, path: string): string {
  const prefix = corpus.allowed_uri_prefixes.find((p) => {
    const u = new URL(p);
    const rel = u.pathname.split("/").filter(Boolean).slice(3).join("/") + "/";
    return path.startsWith(rel);
  });
  if (!prefix) throw new KnowledgeError("E_URI_FORBIDDEN", `tree path ${path} matches no allowed prefix`);
  const u = new URL(prefix);
  const base = u.pathname.split("/").filter(Boolean).slice(0, 3).join("/");
  return `https://${u.host}/${base}/${path}`;
}

export async function reingestCorpus(
  store: Store,
  config: Config,
  corpus: Corpus,
  callerId: string,
  opts: { fetcher?: Fetcher; now?: () => number } = {},
): Promise<ReingestResult> {
  if (!corpus.tree_source) {
    throw new KnowledgeError("E_UNSUPPORTED", `corpus ${corpus.name} declares no tree_source; reingest needs one`);
  }
  const fetcher = opts.fetcher ?? defaultFetcher;
  const now = opts.now ?? Date.now;

  let treeRes: { status: number; text: string };
  try {
    treeRes = await fetcher(`${corpus.tree_source}?recursive=1`, { timeoutMs: config.fetchTimeoutMs, maxBytes: 2_097_152 });
  } catch (err) {
    if (err instanceof KnowledgeError) throw err;
    throw new KnowledgeError("E_URI_UNREACHABLE", "tree source fetch failed", true);
  }
  if (treeRes.status !== 200) throw new KnowledgeError("E_URI_UNREACHABLE", `tree source returned ${treeRes.status}`, true);

  let tree: { sha?: string; tree?: TreeEntry[] };
  try {
    tree = JSON.parse(treeRes.text);
  } catch {
    throw new KnowledgeError("E_URI_UNREACHABLE", "tree source returned unparseable JSON", true);
  }
  const prefixes = pathPrefixes(corpus);
  const wanted = (tree.tree ?? [])
    .filter((e) => e.type === "blob" && e.path.endsWith(".md") && prefixes.some((p) => e.path.startsWith(p)))
    .map((e) => e.path);

  const result: ReingestResult = {
    corpus: corpus.name,
    source_ref: tree.sha ?? null,
    indexed: 0,
    unchanged: 0,
    tombstoned: 0,
    errors: [],
  };

  const liveUris = new Set<string>();
  for (const path of wanted) {
    const uri = rawUriFor(corpus, path);
    liveUris.add(uri);
    try {
      const r = await ingestUri(store, config, corpus, uri, callerId, {
        fetcher,
        sourceCommit: tree.sha ?? null,
        now,
      });
      if (r.status === "indexed") result.indexed++;
      else result.unchanged++;
    } catch (err) {
      // One bad document must not abort the sweep; per-doc transactions mean
      // nothing is half-applied. At-least-once + sha idempotency make the
      // next run heal whatever failed here.
      const code = err instanceof KnowledgeError ? err.code : "E_INTERNAL";
      result.errors.push({ uri, code });
    }
  }

  // Tombstone vanished docs — but never on a partial sweep's evidence alone:
  // a doc is tombstoned only when the tree listing succeeded (we are here) and
  // its uri is absent from that listing.
  for (const live of store.liveDocs(corpus.name)) {
    if (!liveUris.has(live.uri)) {
      store.tombstone(live.doc_id, now());
      result.tombstoned++;
    }
  }

  store.setCorpusMeta(corpus.name, result.source_ref, now());
  return result;
}
