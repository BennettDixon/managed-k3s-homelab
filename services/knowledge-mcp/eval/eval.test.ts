import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import { openDb } from "../src/db.js";
import { Store, sha256 } from "../src/store.js";
import { normalizeContent } from "../src/ingest.js";
import type { Corpus } from "../src/registry.js";

// Retrieval eval (spec §5): a scratch index of the WORKING TREE's docs/ +
// proxmox/, scored against golden queries. recall@5 on expect entries is a
// hard gate; expect_miss entries are the measured lexical blind spot — a miss
// that starts hitting is reported as an improvement, never a failure.

const REPO_ROOT = resolve(import.meta.dirname, "../../..");
const CORPUS_DIRS = ["docs", "proxmox"];
const K = 5;

interface GoldenQuery {
  q: string;
  expect: string[];
  expect_miss?: boolean;
}

function mdFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) out.push(...mdFiles(p));
    else if (p.endsWith(".md")) out.push(p);
  }
  return out;
}

function buildScratchIndex(): Store {
  const store = new Store(openDb(":memory:"));
  for (const dir of CORPUS_DIRS) {
    for (const path of mdFiles(join(REPO_ROOT, dir))) {
      const rel = relative(REPO_ROOT, path);
      const content = normalizeContent(readFileSync(path, "utf8"));
      store.upsertDoc({
        doc_id: `homelab-notes:${rel}`,
        corpus: "homelab-notes",
        uri: `eval://${rel}`,
        title: null,
        content,
        content_sha256: sha256(content),
        bytes: Buffer.byteLength(content, "utf8"),
        source_commit: "working-tree",
        trust: "operator-authored",
        ingested_by: "eval",
        ingested_at: 0,
      });
    }
  }
  return store;
}

describe("golden retrieval eval", () => {
  const store = buildScratchIndex();
  const corpus = { name: "homelab-notes" } as Corpus;
  const golden = (parse(readFileSync(join(import.meta.dirname, "golden.yaml"), "utf8")) as { queries: GoldenQuery[] }).queries;

  const hits: string[] = [];
  const misses: string[] = [];
  const improvements: string[] = [];
  let reciprocalSum = 0;
  let scored = 0;

  for (const g of golden) {
    const results = store.search(corpus, g.q, K);
    const rank = results.findIndex((r) => g.expect.includes(r.doc_id));
    const hit = rank >= 0;
    if (!g.expect_miss) {
      scored++;
      if (hit) {
        hits.push(g.q);
        reciprocalSum += 1 / (rank + 1);
      } else {
        misses.push(g.q);
      }
    } else if (hit) {
      improvements.push(g.q);
    }
  }

  it("every expect query lands its doc in the top-5 (hard gate)", () => {
    // eslint-disable-next-line no-console
    console.log(
      `eval: recall@${K} ${hits.length}/${scored}, MRR ${(reciprocalSum / Math.max(1, scored)).toFixed(3)}` +
        (improvements.length ? `; expect_miss now hitting (promote them): ${improvements.join(" | ")}` : ""),
    );
    expect(misses, `queries that lost their doc from top-${K}`).toEqual([]);
  });

  it("keeps the blind spot measured: expect_miss entries exist", () => {
    expect(golden.some((g) => g.expect_miss)).toBe(true);
  });
});
