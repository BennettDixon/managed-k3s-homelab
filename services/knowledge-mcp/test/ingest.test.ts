import { describe, expect, it } from "vitest";
import { ingestUri, normalizeContent, docIdFor } from "../src/ingest.js";
import { KnowledgeError } from "../src/errors.js";
import { testStore, testRegistry, testConfig, cannedFetcher, RAW_PREFIX } from "./helpers.js";

const registry = testRegistry();
const corpus = registry.corpora.get("homelab-notes")!;
const config = testConfig();
const DOC_URI = `${RAW_PREFIX}docs/runbooks/jobs-mcp.md`;

describe("uri allow-list (spec §6 — the enforcement point)", () => {
  it("rejects non-https and off-prefix uris with E_URI_FORBIDDEN", async () => {
    const { store } = testStore();
    await expect(ingestUri(store, config, corpus, "http://raw.githubusercontent.com/x", "operator")).rejects.toMatchObject({ code: "E_URI_FORBIDDEN" });
    await expect(ingestUri(store, config, corpus, "https://evil.example.com/docs/a.md", "operator")).rejects.toMatchObject({ code: "E_URI_FORBIDDEN" });
    await expect(
      ingestUri(store, config, corpus, `${RAW_PREFIX}terraform/main.tf`, "operator"),
    ).rejects.toMatchObject({ code: "E_URI_FORBIDDEN" });
  });

  it("a prefix-satisfying uri is fetched and indexed", async () => {
    const { store } = testStore();
    const fetcher = cannedFetcher({ [DOC_URI]: "# Runbook\n\n## Section\n\n" + "content ".repeat(50) });
    const r = await ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher });
    expect(r.status).toBe("indexed");
    expect(r.doc_id).toBe("homelab-notes:docs/runbooks/jobs-mcp.md");
    expect(store.getDoc(r.doc_id)!.trust).toBe("operator-authored");
    expect(store.getDoc(r.doc_id)!.ingested_by).toBe("operator");
  });

  it("second ingest of identical content short-circuits as unchanged", async () => {
    const { store } = testStore();
    const fetcher = cannedFetcher({ [DOC_URI]: "# R\n\n## S\n\n" + "c ".repeat(200) });
    await ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher });
    const again = await ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher });
    expect(again.status).toBe("unchanged");
  });

  it("404 maps to E_NOT_FOUND; other statuses to retryable E_URI_UNREACHABLE", async () => {
    const { store } = testStore();
    await expect(ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher: cannedFetcher({}) })).rejects.toMatchObject({ code: "E_NOT_FOUND" });
    const flaky = cannedFetcher({ [DOC_URI]: { status: 503, text: "" } });
    await expect(ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher: flaky })).rejects.toMatchObject({
      code: "E_URI_UNREACHABLE",
      retryable: true,
    });
  });

  it("fetcher exceptions (redirects, timeouts) surface as retryable E_URI_UNREACHABLE", async () => {
    const { store } = testStore();
    const throwing = async () => {
      throw new TypeError("fetch failed: unexpected redirect");
    };
    await expect(ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher: throwing })).rejects.toMatchObject({
      code: "E_URI_UNREACHABLE",
      retryable: true,
    });
  });

  it("oversized documents are rejected with E_DOC_TOO_LARGE", async () => {
    const { store } = testStore();
    const fetcher = cannedFetcher({ [DOC_URI]: "x".repeat(300_000) });
    await expect(ingestUri(store, config, corpus, DOC_URI, "operator", { fetcher })).rejects.toMatchObject({ code: "E_DOC_TOO_LARGE" });
  });
});

describe("normalization (spec §6)", () => {
  it("strips control chars, zero-width, and Unicode tag codepoints; keeps \\n and \\t", () => {
    const dirty = "a\u0000b\u200Bc\u{E0041}d\ne\tf\u0007g";
    expect(normalizeContent(dirty)).toBe("abcd\ne\tfg");
  });

  it("normalizes CRLF", () => {
    expect(normalizeContent("a\r\nb")).toBe("a\nb");
  });
});

describe("doc ids (spec §5)", () => {
  it("derives corpus:repo-relative-path", () => {
    expect(docIdFor(corpus, `${RAW_PREFIX}proxmox/edgepve.md`)).toBe("homelab-notes:proxmox/edgepve.md");
  });
});
