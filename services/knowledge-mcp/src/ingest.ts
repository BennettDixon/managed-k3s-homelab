import { KnowledgeError } from "./errors.js";
import type { Config } from "./config.js";
import type { Corpus } from "./registry.js";
import { Store, sha256 } from "./store.js";

// Ingest (spec §6): inline, synchronous, pinned fetch. The allowlist is the
// enforcement point — https only, exact PR-gated prefix match, redirects
// DISABLED (a prefix-satisfying URL that redirects elsewhere is a bypass, so
// any redirect is a hard error), size-capped, timeout-bounded, no auth
// headers ever attached. Host pinning is supplied by TLS certificate
// validation on the allowlisted host (an rebinding attacker without a valid
// cert for the pinned name gets a handshake failure, not our request).

// Control chars (minus \n\t), Unicode tag block (hidden-instruction
// smuggling), and zero-width/invisible formatting codepoints (spec §6).
// Also stripped since the 2026-09-02 review: bare \r (post-CRLF-collapse),
// the C1 block, and bidi overrides/isolates (Trojan-Source-class hidden text).
const STRIP_RE =
  /[\u0000-\u0008\u000B-\u000D\u000E-\u001F\u007F-\u009F]|[\u200B-\u200F\u2060\uFEFF]|[\u202A-\u202E\u2066-\u2069]|[\u{E0000}-\u{E007F}]/gu;

export function normalizeContent(raw: string): string {
  return raw.replace(/\r\n/g, "\n").replace(STRIP_RE, "");
}

// The prefix check runs on the CANONICALIZED url (URL parsing resolves dot
// segments), never the raw string — "…/main/docs/../../../other/repo/x.md"
// passes a raw startsWith but actually addresses a different path on the same
// host. Queries and fragments are rejected outright: no allowlisted source
// needs them, and they widen what a prefix can be smuggled past.
export function canonicalizeUri(uri: string): string {
  let u: URL;
  try {
    u = new URL(uri);
  } catch {
    throw new KnowledgeError("E_URI_FORBIDDEN", "uri is not a valid URL");
  }
  if (u.search !== "" || u.hash !== "") throw new KnowledgeError("E_URI_FORBIDDEN", "uris with query or fragment are not accepted");
  if (u.username !== "" || u.password !== "") throw new KnowledgeError("E_URI_FORBIDDEN", "uris with credentials are not accepted");
  // No percent-encoding at all: repo .md paths never need it, and rejecting
  // "%" outright closes the whole decode-count class (double-encoding, %00,
  // malformed escapes) in one check — review finding, 2026-09-02.
  if (u.pathname.includes("%")) throw new KnowledgeError("E_URI_FORBIDDEN", "uri path contains percent-encoding");
  if (u.pathname.includes("//") || u.pathname.includes("..")) {
    throw new KnowledgeError("E_URI_FORBIDDEN", "uri path contains dot segments or empty segments");
  }
  return u.href;
}

export function uriAllowed(uri: string, corpus: Corpus): boolean {
  return corpus.allowed_uri_prefixes.some((p) => uri.startsWith(p));
}

// Repo-relative doc path from an allowlisted raw URI: everything after the
// prefix's last path segment start. doc_id = "<corpus>:<relative-path>".
export function docIdFor(corpus: Corpus, uri: string): string {
  const prefix = corpus.allowed_uri_prefixes.find((p) => uri.startsWith(p));
  if (!prefix) throw new KnowledgeError("E_URI_FORBIDDEN", "uri is outside the corpus allow-list");
  // Prefixes pin to .../main/<dir>/ — recover "<dir>/<rest>" for legibility.
  const u = new URL(uri);
  const parts = u.pathname.split("/").filter(Boolean);
  // raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>
  const rel = parts.slice(3).join("/");
  return `${corpus.name}:${rel}`;
}

export interface IngestResult {
  doc_id: string;
  status: "indexed" | "unchanged";
  content_sha256: string;
}

export type Fetcher = (url: string, opts: { timeoutMs: number; maxBytes: number }) => Promise<{ status: number; text: string }>;

// Default fetcher: undici fetch with redirects hard-disabled and a byte cap
// enforced while streaming (content-length alone is attacker-controlled).
export const defaultFetcher: Fetcher = async (url, { timeoutMs, maxBytes }) => {
  const res = await fetch(url, {
    redirect: "error",
    signal: AbortSignal.timeout(timeoutMs),
    headers: { accept: "text/plain, text/markdown, application/json" },
  });
  if (!res.body) return { status: res.status, text: "" };
  const reader = res.body.getReader();
  const bufs: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      void reader.cancel();
      throw new KnowledgeError("E_DOC_TOO_LARGE", `response exceeds ${maxBytes} bytes`);
    }
    bufs.push(value);
  }
  return { status: res.status, text: Buffer.concat(bufs).toString("utf8") };
};

export async function ingestUri(
  store: Store,
  config: Config,
  corpus: Corpus,
  uri: string,
  callerId: string,
  opts: { fetcher?: Fetcher; sourceCommit?: string | null; now?: () => number } = {},
): Promise<IngestResult> {
  const fetcher = opts.fetcher ?? defaultFetcher;
  const now = opts.now ?? Date.now;
  if (!uri.startsWith("https://")) throw new KnowledgeError("E_URI_FORBIDDEN", "only https uris are accepted");
  uri = canonicalizeUri(uri);
  if (!uriAllowed(uri, corpus)) throw new KnowledgeError("E_URI_FORBIDDEN", "uri is outside the corpus allow-list");

  let fetched: { status: number; text: string };
  try {
    fetched = await fetcher(uri, { timeoutMs: config.fetchTimeoutMs, maxBytes: corpus.max_doc_bytes });
  } catch (err) {
    if (err instanceof KnowledgeError) throw err;
    // Includes redirect: "error" rejections and timeouts — retryable for the
    // caller, and never a hint about internal network shape.
    throw new KnowledgeError("E_URI_UNREACHABLE", "source fetch failed", true);
  }
  if (fetched.status === 404) throw new KnowledgeError("E_NOT_FOUND", "source document not found");
  if (fetched.status !== 200) throw new KnowledgeError("E_URI_UNREACHABLE", `source returned ${fetched.status}`, true);

  const content = normalizeContent(fetched.text);
  const bytes = Buffer.byteLength(content, "utf8");
  if (bytes > corpus.max_doc_bytes) throw new KnowledgeError("E_DOC_TOO_LARGE", `document exceeds corpus max_doc_bytes`);
  const contentSha = sha256(content);
  const docId = docIdFor(corpus, uri);

  const status = store.upsertDoc({
    doc_id: docId,
    corpus: corpus.name,
    uri,
    title: null,
    content,
    content_sha256: contentSha,
    bytes,
    source_commit: opts.sourceCommit ?? null,
    trust: corpus.trust,
    ingested_by: callerId,
    ingested_at: now(),
  });
  return { doc_id: docId, status, content_sha256: contentSha };
}
