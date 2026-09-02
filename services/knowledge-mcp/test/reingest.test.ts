import { describe, expect, it } from "vitest";
import { reingestCorpus } from "../src/reingest.js";
import { testStore, testRegistry, testConfig, cannedFetcher, RAW_PREFIX, seedDoc } from "./helpers.js";

const registry = testRegistry();
const corpus = registry.corpora.get("homelab-notes")!;
const config = testConfig();
const TREE_URL = "https://api.github.com/repos/BennettDixon/managed-k3s-homelab/git/trees/main?recursive=1";

function treeJson(paths: string[], sha = "commit-sha-1"): string {
  return JSON.stringify({ sha, tree: paths.map((p) => ({ path: p, type: "blob" })) });
}

describe("reingest (spec §6 — the freshness mechanism)", () => {
  it("walks the tree, ingests matching .md docs, records the source ref", async () => {
    const { store } = testStore();
    const fetcher = cannedFetcher({
      [TREE_URL]: treeJson(["docs/STATUS.md", "proxmox/edgepve.md", "docs/img.png", "terraform/main.tf", "README.md"]),
      [`${RAW_PREFIX}docs/STATUS.md`]: "# STATUS\n\n## Standing\n\n" + "s ".repeat(200),
      [`${RAW_PREFIX}proxmox/edgepve.md`]: "# edgepve\n\n## Wiring\n\n" + "w ".repeat(200),
    });
    const r = await reingestCorpus(store, config, corpus, "n8n-reingest", { fetcher });
    expect(r.indexed).toBe(2);
    expect(r.unchanged).toBe(0);
    expect(r.tombstoned).toBe(0);
    expect(r.errors).toEqual([]);
    expect(r.source_ref).toBe("commit-sha-1");
    expect(store.corpusMeta("homelab-notes").source_ref).toBe("commit-sha-1");
    expect(store.getDoc("homelab-notes:docs/STATUS.md")!.source_commit).toBe("commit-sha-1");
    // Off-prefix and non-.md tree entries were never fetched or indexed.
    expect(store.getDoc("homelab-notes:terraform/main.tf")).toBeUndefined();
    expect(store.getDoc("homelab-notes:README.md")).toBeUndefined();
  });

  it("second run over an unchanged tree is all unchanged (sha short-circuit)", async () => {
    const { store } = testStore();
    const fetcher = cannedFetcher({
      [TREE_URL]: treeJson(["docs/STATUS.md"]),
      [`${RAW_PREFIX}docs/STATUS.md`]: "# STATUS\n\n## S\n\n" + "s ".repeat(200),
    });
    await reingestCorpus(store, config, corpus, "n8n-reingest", { fetcher });
    const r2 = await reingestCorpus(store, config, corpus, "n8n-reingest", { fetcher });
    expect(r2.indexed).toBe(0);
    expect(r2.unchanged).toBe(1);
  });

  it("tombstones docs that vanished from the tree — only after a successful listing", async () => {
    const { store } = testStore();
    seedDoc(store, "homelab-notes:docs/gone.md", "homelab-notes", `${RAW_PREFIX}docs/gone.md`, "# Gone\n\n## S\n\n" + "g ".repeat(200));
    const fetcher = cannedFetcher({
      [TREE_URL]: treeJson(["docs/STATUS.md"]),
      [`${RAW_PREFIX}docs/STATUS.md`]: "# STATUS\n\n## S\n\n" + "s ".repeat(200),
    });
    const r = await reingestCorpus(store, config, corpus, "n8n-reingest", { fetcher });
    expect(r.tombstoned).toBe(1);
    expect(store.getDoc("homelab-notes:docs/gone.md")!.tombstoned_at).not.toBeNull();
  });

  it("a failing document does not abort the sweep and is reported", async () => {
    const { store } = testStore();
    const fetcher = cannedFetcher({
      [TREE_URL]: treeJson(["docs/STATUS.md", "docs/broken.md"]),
      [`${RAW_PREFIX}docs/STATUS.md`]: "# STATUS\n\n## S\n\n" + "s ".repeat(200),
      [`${RAW_PREFIX}docs/broken.md`]: { status: 503, text: "" },
    });
    const r = await reingestCorpus(store, config, corpus, "n8n-reingest", { fetcher });
    expect(r.indexed).toBe(1);
    expect(r.errors).toEqual([{ uri: `${RAW_PREFIX}docs/broken.md`, code: "E_URI_UNREACHABLE" }]);
    // The failed doc must NOT be tombstoned — it is still in the tree.
    expect(r.tombstoned).toBe(0);
  });

  it("an unreachable tree source aborts with a retryable error and tombstones nothing", async () => {
    const { store } = testStore();
    seedDoc(store, "homelab-notes:docs/keep.md", "homelab-notes", `${RAW_PREFIX}docs/keep.md`, "# K\n\n## S\n\n" + "k ".repeat(200));
    await expect(reingestCorpus(store, config, corpus, "n8n-reingest", { fetcher: cannedFetcher({}) })).rejects.toMatchObject({
      code: "E_URI_UNREACHABLE",
      retryable: true,
    });
    expect(store.liveDocs("homelab-notes")).toHaveLength(1);
  });

  it("a corpus without tree_source refuses reingest with E_UNSUPPORTED", async () => {
    const { store } = testStore();
    const faq = registry.corpora.get("public-faq")!;
    await expect(reingestCorpus(store, config, faq, "operator", { fetcher: cannedFetcher({}) })).rejects.toMatchObject({ code: "E_UNSUPPORTED" });
  });
});
