"""SQLite backing store (spec §1): one file, WAL, synchronous=FULL, additive migrations.

The connection is opened with ``isolation_level=None`` — no implicit
transactions — so every money transition is an explicit ``BEGIN IMMEDIATE …
COMMIT`` in ledger.py, never a stdlib-managed one that would run the SELECT
outside the write lock.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "1"

# Spec §1 DDL verbatim (IF NOT EXISTS: additive-only within a minor version).
# No column can hold prompt or completion text, a prompt hash, or the OpenAI
# `user` field — the request-log posture (§9.3) is structural.
MIGRATIONS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS requests (
      id TEXT PRIMARY KEY,
      caller_id TEXT NOT NULL, class TEXT NOT NULL, project_id TEXT NOT NULL,
      job_id TEXT,
      period TEXT NOT NULL, day TEXT NOT NULL,
      lane_requested TEXT NOT NULL, lane_used TEXT, fallback INTEGER NOT NULL DEFAULT 0,
      model TEXT NOT NULL, model_used TEXT,
      state TEXT NOT NULL,
      cap_presented_micro INTEGER NOT NULL,
      reserved_micro INTEGER NOT NULL,
      settled_micro INTEGER,
      list_reserved_micro INTEGER NOT NULL,
      list_micro INTEGER,
      input_tokens INTEGER, output_tokens INTEGER, cache_write_5m_tokens INTEGER,
      cache_write_1h_tokens INTEGER, cache_read_tokens INTEGER, inference_geo TEXT,
      max_tokens INTEGER NOT NULL,
      upstream_started INTEGER NOT NULL DEFAULT 0,
      error_code TEXT, http_status INTEGER, provider_request_id TEXT, latency_ms INTEGER,
      worker_id TEXT, lease_expires_at INTEGER,
      reserved_at INTEGER NOT NULL, settled_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_req_scope  ON requests(project_id, period, state)",
    "CREATE INDEX IF NOT EXISTS idx_req_day    ON requests(caller_id, day, state)",
    "CREATE INDEX IF NOT EXISTS idx_req_job    ON requests(caller_id, job_id) WHERE job_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_req_window ON requests(project_id, lane_used, reserved_at)",
    "CREATE INDEX IF NOT EXISTS idx_req_lease  ON requests(state, lane_used, lease_expires_at)",
    """CREATE TABLE IF NOT EXISTS job_caps (
      caller_id TEXT NOT NULL, job_id TEXT NOT NULL, cap_micro INTEGER NOT NULL,
      first_seen INTEGER NOT NULL, PRIMARY KEY (caller_id, job_id)
    )""",
    """CREATE TABLE IF NOT EXISTS scope_totals (
      scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL, period_key TEXT NOT NULL, basis TEXT NOT NULL,
      held_micro INTEGER NOT NULL DEFAULT 0, settled_micro INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (scope_kind, scope_id, period_key, basis)
    )""",
    """CREATE TABLE IF NOT EXISTS lane_state (
      lane TEXT PRIMARY KEY, last_heartbeat INTEGER, cooling_until INTEGER, auth_ok INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS brakes (
      lane TEXT PRIMARY KEY, tripped_at INTEGER, reason TEXT, reset_at INTEGER, reset_by TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
)


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in MIGRATIONS:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return conn
