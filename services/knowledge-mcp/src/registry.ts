import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { z } from "zod";

// Corpus + caller registry (spec §4). Repo-owned ConfigMap; parse failure
// fails readiness (index.ts), and the admission rules below are enforced at
// parse time so a bad PR can never half-apply.

export type Visibility = "operator" | "frontend";
export type Trust = "operator-authored" | "curated" | "untrusted";
export type CallerClass = "operator" | "reingest-bot" | "frontend";

const corpusSchema = z
  .object({
    description: z.string().min(1),
    rebuild_source: z.enum(["git", "nas"]),
    visibility: z.enum(["operator", "frontend"]).default("operator"),
    trust: z.enum(["operator-authored", "curated", "untrusted"]).default("untrusted"),
    allowed_uri_prefixes: z.array(z.string().url()).min(1),
    tree_source: z.string().url().optional(),
    max_doc_bytes: z.number().int().min(1024).max(1_048_576).default(262_144),
  })
  .strict();

const callerSchema = z
  .object({
    class: z.enum(["operator", "reingest-bot", "frontend"]),
    corpora: z.array(z.string()).optional(),
  })
  .strict();

const registrySchema = z
  .object({
    corpora: z.record(z.string().regex(/^[a-z0-9][a-z0-9-]{1,62}$/), corpusSchema),
    callers: z.record(z.string().regex(/^[a-z0-9][a-z0-9-]{1,62}$/), callerSchema),
  })
  .strict();

export type Corpus = z.infer<typeof corpusSchema> & { name: string };
export type Caller = z.infer<typeof callerSchema> & { id: string };

export interface Registry {
  corpora: Map<string, Corpus>;
  callers: Map<string, Caller>;
}

// Deny-by-default capability table (spec §3): resolved before any handler
// runs. A tool absent from a class's list does not exist for that class.
export const CLASS_TOOLS: Record<CallerClass, ReadonlySet<string>> = {
  operator: new Set(["search", "fetch", "ingest", "reingest", "list_corpora"]),
  "reingest-bot": new Set(["reingest", "list_corpora"]),
  frontend: new Set(["search", "fetch", "list_corpora"]),
};

export function parseRegistry(text: string): Registry {
  const raw = registrySchema.parse(parse(text));
  const corpora = new Map<string, Corpus>();
  for (const [name, c] of Object.entries(raw.corpora)) {
    // Admission (spec §4): quarantine is structural — a poisoned untrusted
    // corpus must be unable to reach a frontend conduit by construction.
    if (c.trust === "untrusted" && c.visibility === "frontend") {
      throw new Error(`corpus ${name}: trust=untrusted may never declare visibility=frontend`);
    }
    for (const p of c.allowed_uri_prefixes) {
      if (!p.startsWith("https://")) throw new Error(`corpus ${name}: allowed_uri_prefixes must be https (${p})`);
    }
    if (c.tree_source && !c.tree_source.startsWith("https://")) {
      throw new Error(`corpus ${name}: tree_source must be https`);
    }
    corpora.set(name, { ...c, name });
  }
  const callers = new Map<string, Caller>();
  for (const [id, caller] of Object.entries(raw.callers)) {
    for (const corpus of caller.corpora ?? []) {
      if (!corpora.has(corpus)) throw new Error(`caller ${id}: unknown corpus ${corpus}`);
    }
    callers.set(id, { ...caller, id });
  }
  if (corpora.size === 0) throw new Error("registry has no corpora");
  if (callers.size === 0) throw new Error("registry has no callers");
  return { corpora, callers };
}

export function loadRegistry(path: string): Registry {
  return parseRegistry(readFileSync(path, "utf8"));
}

// Corpora visible to a caller class (spec §4): frontend sees only
// visibility=frontend; operator classes see everything. Computed FIRST, never
// post-filtered (a buggy post-filter leaks counts and snippets).
export function visibleCorpora(registry: Registry, caller: Caller): Map<string, Corpus> {
  const out = new Map<string, Corpus>();
  for (const [name, corpus] of registry.corpora) {
    if (caller.corpora && !caller.corpora.includes(name)) continue;
    if (caller.class === "frontend" && corpus.visibility !== "frontend") continue;
    out.set(name, corpus);
  }
  return out;
}
