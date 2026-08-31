import Database from "better-sqlite3";

// Additive-only migrations within a minor version (spec §1): a one-step
// image rollback must reopen the DB safely.
const MIGRATIONS: string[] = [
  `CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    task_type       TEXT NOT NULL,
    payload         TEXT NOT NULL,
    budget_cap_usd  REAL NOT NULL,
    priority        INTEGER NOT NULL,
    artifacts_out   TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    envelope_hash   TEXT,
    state           TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL,
    not_before      INTEGER,
    error           TEXT,
    result          TEXT,
    spent_usd       REAL,
    artifacts       TEXT,
    enqueued_at     INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER
  )`,
  `CREATE INDEX IF NOT EXISTS idx_dispatch ON jobs(state, priority, id)`,
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

// Readiness write-ping (spec §8): a real committed write, kept off the
// liveness path so a full disk degrades to NotReady, never CrashLoopBackOff.
export function writePing(db: Database.Database): void {
  db.prepare("INSERT INTO meta (k, v) VALUES ('ping', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v").run(String(Date.now()));
}
