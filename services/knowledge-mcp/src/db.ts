import Database from "better-sqlite3";

// Additive-only migrations within a minor version (spec §1): a one-step image
// rollback must reopen the DB safely. A tokenizer change is a REBUILD (index
// is a cache), never a migration — spec §1 says so in words; this array must
// never learn to ALTER the fts5 table.
const MIGRATIONS: string[] = [
  `CREATE TABLE IF NOT EXISTS docs (
    doc_id         TEXT PRIMARY KEY,
    corpus         TEXT NOT NULL,
    uri            TEXT NOT NULL,
    title          TEXT,
    content        TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    bytes          INTEGER NOT NULL,
    source_commit  TEXT,
    trust          TEXT NOT NULL,
    ingested_by    TEXT NOT NULL,
    ingested_at    INTEGER NOT NULL,
    tombstoned_at  INTEGER
  )`,
  `CREATE INDEX IF NOT EXISTS idx_docs_corpus ON docs(corpus, tombstoned_at)`,
  // doc_id/chunk_index/content_sha256 are UNINDEXED: they are join keys and
  // change-detection state, and tokenizing them into the FTS index pollutes
  // term statistics (verified during design review).
  `CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    title, heading_path, body,
    doc_id UNINDEXED, chunk_index UNINDEXED, content_sha256 UNINDEXED,
    tokenize = 'unicode61'
  )`,
  `CREATE TABLE IF NOT EXISTS corpus_meta (
    corpus      TEXT PRIMARY KEY,
    source_ref  TEXT,
    ingested_at INTEGER
  )`,
  `CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)`,
];

export function openDb(path: string): Database.Database {
  const db = new Database(path);
  db.pragma("journal_mode = WAL");
  db.pragma("synchronous = FULL");
  const migrate = db.transaction(() => {
    for (const sql of MIGRATIONS) db.exec(sql);
  });
  migrate();
  return db;
}

// Readiness is a READ ping — the deliberate deviation from jobs-mcp (spec §8):
// this service is read-mostly, and a full disk must degrade *ingest* (error +
// metric), never take *search* down. Nothing on any probe path writes.
export function readPing(db: Database.Database): void {
  db.prepare("SELECT 1").get();
}
