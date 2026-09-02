import { createHash } from "node:crypto";
import type Database from "better-sqlite3";
import { chunkMarkdown } from "./chunker.js";
import { buildMatch, normalizeScore } from "./query.js";
import { KnowledgeError } from "./errors.js";
import type { Corpus } from "./registry.js";

export interface DocRow {
  doc_id: string;
  corpus: string;
  uri: string;
  title: string | null;
  content: string;
  content_sha256: string;
  bytes: number;
  source_commit: string | null;
  trust: string;
  ingested_by: string;
  ingested_at: number;
  tombstoned_at: number | null;
}

export interface SearchHit {
  chunk_id: string;
  doc_id: string;
  path: string;
  heading_path: string;
  text: string;
  truncated: boolean;
  score: number;
  rank: number;
  trust: string;
  source_commit: string | null;
  neighbors: { prev: string | null; next: string | null };
}

export function sha256(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

export class Store {
  private upsertDocStmt;
  private deleteChunksStmt;
  private insertChunkStmt;
  private getDocStmt;
  private setCorpusMetaStmt;

  constructor(private db: Database.Database) {
    this.upsertDocStmt = db.prepare(
      `INSERT INTO docs (doc_id, corpus, uri, title, content, content_sha256, bytes, source_commit, trust, ingested_by, ingested_at, tombstoned_at)
       VALUES (@doc_id, @corpus, @uri, @title, @content, @content_sha256, @bytes, @source_commit, @trust, @ingested_by, @ingested_at, NULL)
       ON CONFLICT(doc_id) DO UPDATE SET
         uri = excluded.uri, title = excluded.title, content = excluded.content,
         content_sha256 = excluded.content_sha256, bytes = excluded.bytes,
         source_commit = excluded.source_commit, trust = excluded.trust,
         ingested_by = excluded.ingested_by, ingested_at = excluded.ingested_at,
         tombstoned_at = NULL`,
    );
    this.deleteChunksStmt = db.prepare(`DELETE FROM chunks WHERE doc_id = ?`);
    this.insertChunkStmt = db.prepare(
      `INSERT INTO chunks (title, heading_path, body, doc_id, chunk_index, content_sha256) VALUES (?, ?, ?, ?, ?, ?)`,
    );
    this.getDocStmt = db.prepare(`SELECT * FROM docs WHERE doc_id = ?`);
    this.setCorpusMetaStmt = db.prepare(
      `INSERT INTO corpus_meta (corpus, source_ref, ingested_at) VALUES (?, ?, ?)
       ON CONFLICT(corpus) DO UPDATE SET source_ref = excluded.source_ref, ingested_at = excluded.ingested_at`,
    );
  }

  // One transaction per document (spec §6/§10): a power cut mid-ingest leaves
  // the previous version fully intact — doc row and chunks always agree.
  upsertDoc(row: Omit<DocRow, "tombstoned_at">): "indexed" | "unchanged" {
    const existing = this.getDoc(row.doc_id);
    if (existing && existing.content_sha256 === row.content_sha256 && existing.tombstoned_at === null) {
      return "unchanged";
    }
    const { title, chunks } = chunkMarkdown(row.doc_id, row.content);
    const tx = this.db.transaction(() => {
      this.upsertDocStmt.run({ ...row, title: row.title ?? title });
      this.deleteChunksStmt.run(row.doc_id);
      for (const c of chunks) {
        this.insertChunkStmt.run(row.title ?? title ?? "", c.headingPath, c.body, row.doc_id, c.chunkIndex, row.content_sha256);
      }
    });
    tx();
    return "indexed";
  }

  getDoc(docId: string): DocRow | undefined {
    return this.getDocStmt.get(docId) as DocRow | undefined;
  }

  // Tombstones are soft (spec §11 non-goals: deletion is a destructive-op
  // task); tombstoned docs vanish from search/fetch but rows remain.
  tombstone(docId: string, now: number): void {
    const tx = this.db.transaction(() => {
      this.db.prepare(`UPDATE docs SET tombstoned_at = ? WHERE doc_id = ?`).run(now, docId);
      this.deleteChunksStmt.run(docId);
    });
    tx();
  }

  liveDocs(corpus: string): Array<{ doc_id: string; uri: string; content_sha256: string }> {
    return this.db
      .prepare(`SELECT doc_id, uri, content_sha256 FROM docs WHERE corpus = ? AND tombstoned_at IS NULL`)
      .all(corpus) as Array<{ doc_id: string; uri: string; content_sha256: string }>;
  }

  setCorpusMeta(corpus: string, sourceRef: string | null, at: number): void {
    this.setCorpusMetaStmt.run(corpus, sourceRef, at);
  }

  corpusMeta(corpus: string): { source_ref: string | null; ingested_at: number | null } {
    const row = this.db.prepare(`SELECT source_ref, ingested_at FROM corpus_meta WHERE corpus = ?`).get(corpus) as
      | { source_ref: string | null; ingested_at: number | null }
      | undefined;
    return row ?? { source_ref: null, ingested_at: null };
  }

  counts(corpus: string): { docs: number; chunks: number } {
    const docs = (this.db.prepare(`SELECT COUNT(*) AS n FROM docs WHERE corpus = ? AND tombstoned_at IS NULL`).get(corpus) as { n: number }).n;
    const chunks = (
      this.db
        .prepare(`SELECT COUNT(*) AS n FROM chunks WHERE doc_id IN (SELECT doc_id FROM docs WHERE corpus = ? AND tombstoned_at IS NULL)`)
        .get(corpus) as { n: number }
    ).n;
    return { docs, chunks };
  }

  // Two-pass retrieval (spec §5): all-terms AND, falling back to OR when AND
  // is empty. Column weights title/heading_path/body = 8/4/1.
  search(corpus: Corpus, query: string, k: number): SearchHit[] {
    const built = buildMatch(query);
    const run = (match: string) => {
      try {
        return this.db
          .prepare(
            `SELECT c.rowid AS rowid, c.doc_id AS doc_id, c.chunk_index AS chunk_index,
                    c.heading_path AS heading_path, c.body AS body,
                    bm25(chunks, 8.0, 4.0, 1.0) AS raw_score
             FROM chunks c
             JOIN docs d ON d.doc_id = c.doc_id
             WHERE chunks MATCH ? AND d.corpus = ? AND d.tombstoned_at IS NULL
             ORDER BY raw_score LIMIT ?`,
          )
          .all(match, corpus.name, k) as Array<{
          rowid: number;
          doc_id: string;
          chunk_index: number;
          heading_path: string;
          body: string;
          raw_score: number;
        }>;
      } catch (err) {
        // A MATCH error after our own query building is a bug, but the caller
        // sees a clean taxonomy error, never an SQL message.
        throw new KnowledgeError("E_QUERY_INVALID", `query could not be executed: ${built.terms.join(" ")}`);
      }
    };
    let rows = run(built.and);
    if (rows.length === 0 && built.terms.length > 1) rows = run(built.or);

    // Response byte discipline (spec §3, ~24KiB): each hit's text is capped
    // to a snippet — the chunk_id makes the full section one fetch away —
    // and trailing hits are dropped once the running budget is spent. MCP
    // results land verbatim in the calling agent's context; the cap is the
    // per-call cost ceiling, enforced here rather than promised.
    const SNIPPET_CAP = 2_048;
    const RESPONSE_BUDGET = 24_576;
    const neighborStmt = this.db.prepare(`SELECT rowid FROM chunks WHERE doc_id = ? AND chunk_index = ?`);
    const hits: SearchHit[] = [];
    let spent = 0;
    for (const [i, r] of rows.entries()) {
      const doc = this.getDoc(r.doc_id)!;
      const truncated = r.body.length > SNIPPET_CAP;
      const text = truncated ? `${r.body.slice(0, SNIPPET_CAP)}…` : r.body;
      if (spent + text.length > RESPONSE_BUDGET && hits.length > 0) break;
      spent += text.length;
      const prev = neighborStmt.get(r.doc_id, r.chunk_index - 1) ? this.chunkIdOf(r.doc_id, r.chunk_index - 1) : null;
      const next = neighborStmt.get(r.doc_id, r.chunk_index + 1) ? this.chunkIdOf(r.doc_id, r.chunk_index + 1) : null;
      hits.push({
        chunk_id: this.chunkIdOf(r.doc_id, r.chunk_index) ?? `${r.doc_id}#${r.chunk_index}`,
        doc_id: r.doc_id,
        path: doc.uri,
        heading_path: r.heading_path,
        text,
        truncated,
        score: normalizeScore(r.raw_score),
        rank: i + 1,
        trust: doc.trust,
        source_commit: doc.source_commit,
        neighbors: { prev, next },
      });
    }
    return hits;
  }

  // Chunk ids are derived (doc_id + slug + ordinal) at chunk time but only the
  // (doc_id, chunk_index) pair is stored in FTS; regenerate the id on demand.
  private chunkIdOf(docId: string, chunkIndex: number): string | null {
    if (chunkIndex < 0) return null;
    const doc = this.getDoc(docId);
    if (!doc) return null;
    const { chunks } = chunkMarkdown(docId, doc.content);
    return chunks[chunkIndex]?.chunkId ?? null;
  }

  chunksOf(docId: string): Array<{ chunk_id: string; heading_path: string; bytes: number }> {
    const doc = this.getDoc(docId);
    if (!doc) return [];
    return chunkMarkdown(docId, doc.content).chunks.map((c) => ({
      chunk_id: c.chunkId,
      heading_path: c.headingPath,
      bytes: Buffer.byteLength(c.body, "utf8"),
    }));
  }

  chunkBody(docId: string, chunkId: string): string | null {
    const doc = this.getDoc(docId);
    if (!doc) return null;
    const chunk = chunkMarkdown(docId, doc.content).chunks.find((c) => c.chunkId === chunkId);
    return chunk?.body ?? null;
  }
}
