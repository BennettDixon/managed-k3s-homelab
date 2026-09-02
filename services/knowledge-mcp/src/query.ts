import { KnowledgeError } from "./errors.js";

// Server-built MATCH strings, always (spec §5). Raw user input in FTS5 MATCH
// is a landmine: hyphenated terms ("jobs-mcp") ALWAYS error (column-filter
// parsing — verified against the house binding), and bare operators (AND/OR/
// NOT/NEAR) change semantics. The server tokenizes the query itself, quotes
// every term, and never passes user text through.

export interface BuiltQuery {
  and: string;
  or: string;
  terms: string[];
}

export function buildMatch(query: string): BuiltQuery {
  const trimmed = query.trim();
  if (trimmed.length === 0) throw new KnowledgeError("E_QUERY_INVALID", "query is empty");
  if (trimmed.length > 512) throw new KnowledgeError("E_QUERY_INVALID", "query exceeds 512 chars");
  // Split on whitespace; strip characters that are neither word-ish nor the
  // identifier punctuation we phrase-quote for.
  const rawTerms = trimmed.split(/\s+/).slice(0, 24);
  const terms: string[] = [];
  for (const raw of rawTerms) {
    const cleaned = raw.replace(/["']/g, "").trim();
    if (cleaned.length === 0) continue;
    terms.push(cleaned);
  }
  if (terms.length === 0) throw new KnowledgeError("E_QUERY_INVALID", "query has no searchable terms");
  // Every term ships double-quoted: quoting makes hyphens/underscores/dots a
  // PHRASE of their sub-tokens ("jobs-mcp" -> "jobs" then "mcp" adjacent),
  // which both neutralizes FTS5 syntax and preserves identifier adjacency.
  const quoted = terms.map((t) => `"${t.replace(/"/g, "")}"`);
  return {
    and: quoted.join(" AND "),
    or: quoted.join(" OR "),
    terms,
  };
}

// FTS5 bm25() is negative-is-better; normalize to a positive descending score
// so clients never learn that implementation detail (spec §5).
export function normalizeScore(bm25: number): number {
  return Math.round(-bm25 * 1000) / 1000;
}
