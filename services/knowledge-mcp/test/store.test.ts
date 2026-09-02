import { describe, expect, it } from "vitest";
import { testStore, seedDoc } from "./helpers.js";
import { sha256 } from "../src/store.js";

const URI = "https://raw.githubusercontent.com/BennettDixon/managed-k3s-homelab/main/docs/a.md";

describe("store transactions (spec §1/§6)", () => {
  it("upsert is sha-idempotent: unchanged content is a no-op", () => {
    const { store } = testStore();
    const content = `# A\n\n## S\n\n${"x".repeat(300)}\n`;
    seedDoc(store, "c:docs/a.md", "c", URI, content);
    const again = store.upsertDoc({
      doc_id: "c:docs/a.md",
      corpus: "c",
      uri: URI,
      title: null,
      content,
      content_sha256: sha256(content),
      bytes: content.length,
      source_commit: "othersha",
      trust: "operator-authored",
      ingested_by: "operator",
      ingested_at: 2000,
    });
    expect(again).toBe("unchanged");
    expect(store.getDoc("c:docs/a.md")!.ingested_at).toBe(1000);
  });

  it("changed content replaces doc and chunks atomically", () => {
    const { store, db } = testStore();
    seedDoc(store, "c:docs/a.md", "c", URI, `# A\n\n## Old\n\n${"o".repeat(300)}\n`);
    seedDoc(store, "c:docs/a.md", "c", URI, `# A\n\n## New\n\n${"n".repeat(300)}\n`);
    const rows = db.prepare("SELECT heading_path FROM chunks WHERE doc_id = ?").all("c:docs/a.md") as Array<{ heading_path: string }>;
    expect(rows).toHaveLength(1);
    expect(rows[0]!.heading_path).toBe("New");
    expect(db.prepare("SELECT COUNT(*) AS n FROM docs").get()).toEqual({ n: 1 });
  });

  it("re-ingesting a tombstoned doc revives it", () => {
    const { store } = testStore();
    const content = `# A\n\n## S\n\n${"x".repeat(300)}\n`;
    seedDoc(store, "c:docs/a.md", "c", URI, content);
    store.tombstone("c:docs/a.md", 5000);
    expect(store.liveDocs("c")).toHaveLength(0);
    seedDoc(store, "c:docs/a.md", "c", URI, content); // same sha but tombstoned → re-indexes
    expect(store.liveDocs("c")).toHaveLength(1);
  });

  it("tombstone removes chunks in the same transaction", () => {
    const { store, db } = testStore();
    seedDoc(store, "c:docs/a.md", "c", URI, `# A\n\n## S\n\n${"x".repeat(300)}\n`);
    store.tombstone("c:docs/a.md", 5000);
    expect(db.prepare("SELECT COUNT(*) AS n FROM chunks").get()).toEqual({ n: 0 });
    expect(store.getDoc("c:docs/a.md")!.tombstoned_at).toBe(5000);
  });

  it("counts and corpus meta round-trip", () => {
    const { store } = testStore();
    seedDoc(store, "c:docs/a.md", "c", URI, `# A\n\n## S\n\n${"x".repeat(300)}\n`);
    store.setCorpusMeta("c", "abc123", 42_000);
    expect(store.counts("c").docs).toBe(1);
    expect(store.corpusMeta("c")).toEqual({ source_ref: "abc123", ingested_at: 42_000 });
  });

  it("chunksOf and chunkBody serve fetch's chunk-list path", () => {
    const { store } = testStore();
    seedDoc(store, "c:docs/a.md", "c", URI, `# A\n\n## One\n\n${"x".repeat(300)}\n\n## Two\n\n${"y".repeat(300)}\n`);
    const chunks = store.chunksOf("c:docs/a.md");
    expect(chunks.map((c) => c.chunk_id)).toEqual(["c:docs/a.md#one", "c:docs/a.md#two"]);
    expect(store.chunkBody("c:docs/a.md", "c:docs/a.md#two")).toContain("y".repeat(50));
    expect(store.chunkBody("c:docs/a.md", "c:docs/a.md#nope")).toBeNull();
  });

  it("caps hit text at the snippet limit and flags truncation", () => {
    const { store } = testStore();
    const registryCorpus = { name: "c" } as never;
    const big = `# A\n\n## Long\n\nneedle ${"filler ".repeat(600)}\n`;
    seedDoc(store, "c:docs/a.md", "c", URI, big);
    const hits = store.search(registryCorpus, "needle", 6);
    expect(hits[0]!.truncated).toBe(true);
    expect(hits[0]!.text.length).toBeLessThanOrEqual(2_100);
    expect(hits[0]!.text.endsWith("…")).toBe(true);
  });

  it("search exposes neighbors as ids in document order", () => {
    const { store } = testStore();
    const registryCorpus = { name: "c" } as never;
    seedDoc(store, "c:docs/a.md", "c", URI, `# A\n\n## One\n\nalpha ${"x".repeat(280)}\n\n## Two\n\nbeta ${"y".repeat(280)}\n\n## Three\n\ngamma ${"z".repeat(280)}\n`);
    const hits = store.search(registryCorpus, "beta", 6);
    expect(hits).toHaveLength(1);
    expect(hits[0]!.neighbors.prev).toBe("c:docs/a.md#one");
    expect(hits[0]!.neighbors.next).toBe("c:docs/a.md#three");
  });
});
