"""The budget ledger (spec §1): reserve → settle | release | timeout | aborted | swept.

Invariants this module owns:

* **Money is reserved before a provider is called and settled from what the
  provider reported; unknown spend settles at the reservation, never at zero.**
* One process-wide mutex around every transition, one connection, explicit
  ``BEGIN IMMEDIATE``. Correctness never rests on the isolation mode: the
  enforcement statement is a single conditional compare-and-add on the
  materialized ``scope_totals`` table, written in the same transaction as
  the request row, and recomputed from ``requests`` at boot.
* Refusals write no row. A late or duplicate completion is logged, never
  re-settled. Release happens only while ``upstream_started = 0``.
* Any ``reserved`` row found at boot is by definition an orphan: swept at the
  reservation before the listener opens.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from gateway.jsonlog import Log
from gateway.money import TokenUsage, affordable_tokens, micro_to_usd_str
from gateway.ulid import UlidFactory

BASES: tuple[str, ...] = ("billed", "list")
CHARGED_STATES: frozenset[str] = frozenset({"settled", "timeout", "aborted", "swept"})
CLOCK_SKEW_MS = 60_000  # spec §1 clock guard
FinishOutcome = Literal["release", "timeout", "aborted"]


def period_day(ts_ms: int) -> tuple[str, str]:
    """('YYYY-MM', 'YYYY-MM-DD') in UTC — fixed at reserve, never recomputed at settle."""
    stamp = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return stamp.strftime("%Y-%m"), stamp.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Scope:
    kind: str
    id: str
    period_key: str


def scopes_for(
    caller_id: str, job_id: str | None, project_id: str, period: str, day: str, lane: str
) -> tuple[Scope, ...]:
    """The scope rows one request row contributes to — derived from its columns only.

    Deterministic by construction so the boot recomputation (``recompute_totals``)
    and every transition agree on exactly which totals a row touches.
    """
    scopes = [
        Scope("project", project_id, period),
        Scope("caller_day", caller_id, day),
        Scope("brake", lane, day),
    ]
    if job_id is not None:
        scopes.append(Scope("job", f"{caller_id}/{job_id}", ""))
    return tuple(scopes)


@dataclass(frozen=True)
class Totals:
    held_billed: int = 0
    settled_billed: int = 0
    held_list: int = 0
    settled_list: int = 0

    def committed(self, basis: str) -> int:
        if basis == "billed":
            return self.held_billed + self.settled_billed
        return self.held_list + self.settled_list


@dataclass(frozen=True)
class ReserveInput:
    caller_id: str
    caller_class: str
    project_id: str
    job_id: str | None
    lane_requested: str
    lane_used: str
    fallback: bool
    model: str
    cap_presented_micro: int
    reserved_micro: int  # billed worst case
    list_reserved_micro: int  # list worst case
    max_tokens: int
    in_tokens: int
    in_cost_billed_micro: int
    in_cost_list_micro: int
    output_price_micro_per_mtok: int
    billed_multiplier_pct: int
    project_cap_micro: int
    caller_day_cap_micro: int | None
    brake_cap_micro: int


@dataclass(frozen=True)
class Reservation:
    id: str
    period: str
    day: str
    reserved_at: int
    reserved_micro: int
    list_reserved_micro: int


@dataclass(frozen=True)
class SettleResult:
    applied: bool
    state: str
    settled_micro: int
    list_micro: int
    over_reserve: bool


@dataclass(frozen=True)
class SweepResult:
    count: int
    swept_billed_micro_by_project: dict[str, int]


@dataclass(frozen=True)
class BrakeState:
    lane: str
    tripped: bool
    tripped_at: int | None
    reason: str | None
    reset_at: int | None
    reset_by: str | None


class BudgetRefusal(Exception):
    """Refused at admission (402): no row, no call."""

    def __init__(
        self,
        scope: str,
        basis: str,
        cap_micro: int,
        remaining_micro: int,
        would_reserve_micro: int,
        affordable_max_tokens: int,
        *,
        latched: bool = False,
    ) -> None:
        super().__init__(f"budget exceeded on scope {scope} ({basis})")
        self.scope = scope
        self.basis = basis
        self.cap_micro = cap_micro
        self.remaining_micro = remaining_micro
        self.would_reserve_micro = would_reserve_micro
        self.affordable_max_tokens = affordable_max_tokens
        self.latched = latched


class JobCapMismatch(Exception):
    def __init__(self, pinned_micro: int) -> None:
        super().__init__("job cap mismatch")
        self.pinned_micro = pinned_micro


class ClockGuardTripped(Exception):
    pass


class LedgerUnavailable(Exception):
    """A SQLite write failed: fail closed — no call happens without a row."""


class Ledger:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now_ms: Callable[[], int],
        log: Log,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._conn = conn
        self._now = now_ms
        self._log = log
        self._new_id: Callable[[], str] = new_id or UlidFactory(now_ms)
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- plumbing

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            with suppress(Exception):
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return None if row is None else str(row["v"])

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))

    def _last_commit_ts(self) -> int | None:
        raw = self._meta_get("last_commit_ts")
        return None if raw is None else int(raw)

    def _touch_commit_ts(self, now: int) -> None:
        last = self._last_commit_ts()
        if last is None or now > last:
            self._meta_set("last_commit_ts", str(now))

    def _reserved_row(self, request_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT id, caller_id, job_id, project_id, period, day, lane_used, state, reserved_micro, "
            "list_reserved_micro, upstream_started, reserved_at FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if row is None or row["state"] != "reserved":
            return None
        return row

    def _row_scopes(self, row: sqlite3.Row) -> tuple[Scope, ...]:
        return scopes_for(
            row["caller_id"], row["job_id"], row["project_id"], row["period"], row["day"], row["lane_used"]
        )

    def _move_totals(self, row: sqlite3.Row, settled_billed: int, settled_list: int) -> None:
        for scope in self._row_scopes(row):
            for basis, reserved, settled in (
                ("billed", row["reserved_micro"], settled_billed),
                ("list", row["list_reserved_micro"], settled_list),
            ):
                self._conn.execute(
                    "UPDATE scope_totals SET held_micro = held_micro - ?, settled_micro = settled_micro + ? "
                    "WHERE scope_kind = ? AND scope_id = ? AND period_key = ? AND basis = ?",
                    (reserved, settled, scope.kind, scope.id, scope.period_key, basis),
                )

    def _totals_locked(self, scope: Scope) -> Totals:
        values = {"billed": (0, 0), "list": (0, 0)}
        for row in self._conn.execute(
            "SELECT basis, held_micro, settled_micro FROM scope_totals "
            "WHERE scope_kind = ? AND scope_id = ? AND period_key = ?",
            (scope.kind, scope.id, scope.period_key),
        ):
            values[row["basis"]] = (row["held_micro"], row["settled_micro"])
        return Totals(
            held_billed=values["billed"][0],
            settled_billed=values["billed"][1],
            held_list=values["list"][0],
            settled_list=values["list"][1],
        )

    def _brake_tripped_locked(self, lane: str, now: int) -> bool:
        row = self._conn.execute("SELECT tripped_at, reset_at FROM brakes WHERE lane = ?", (lane,)).fetchone()
        if row is None or row["tripped_at"] is None or row["reset_at"] is not None:
            return False
        # A trip clears itself at 00:00 UTC (spec §5); a reset clears it explicitly
        # (a new trip nulls reset_at, so the latch never depends on clock resolution).
        return period_day(row["tripped_at"])[1] == period_day(now)[1]

    # ----------------------------------------------------------------- reserve

    def reserve(self, inp: ReserveInput) -> Reservation:
        with self._lock:
            try:
                return self._reserve_locked(inp)
            except BudgetRefusal as refusal:
                if refusal.scope.startswith("brake_") and not refusal.latched:
                    self._trip_brake_locked(inp.lane_used, "daily ceiling reached")
                raise
            except sqlite3.Error as err:
                raise LedgerUnavailable(str(err)) from err

    def _affordable(self, inp: ReserveInput, basis: str, remaining: int) -> int:
        in_cost = inp.in_cost_billed_micro if basis == "billed" else inp.in_cost_list_micro
        multiplier = inp.billed_multiplier_pct if basis == "billed" else 100
        return affordable_tokens(remaining - in_cost, inp.output_price_micro_per_mtok, multiplier)

    def _reserve_locked(self, inp: ReserveInput) -> Reservation:
        now = self._now()
        with self._tx():
            # Clock guard (spec §1): a node booting on a stale RTC after a power
            # cut must not reopen a period.
            last = self._last_commit_ts()
            if last is not None and now < last - CLOCK_SKEW_MS:
                raise ClockGuardTripped(f"now {now} is behind last commit {last}")
            period, day = period_day(now)
            if last is not None and period < period_day(last)[0]:
                raise ClockGuardTripped(f"period {period} sorts before the last committed period")

            brake_scope_name = f"brake_{inp.lane_used}"
            if self._brake_tripped_locked(inp.lane_used, now):
                brake = self._totals_locked(Scope("brake", inp.lane_used, day))
                remaining = max(0, inp.brake_cap_micro - brake.committed("billed"))
                raise BudgetRefusal(
                    brake_scope_name,
                    "billed",
                    inp.brake_cap_micro,
                    remaining,
                    inp.reserved_micro,
                    self._affordable(inp, "billed", remaining),
                    latched=True,
                )

            if inp.job_id is not None:
                # The job-cap pin: first request fixes the cap for (caller, job).
                self._conn.execute(
                    "INSERT OR IGNORE INTO job_caps (caller_id, job_id, cap_micro, first_seen) VALUES (?, ?, ?, ?)",
                    (inp.caller_id, inp.job_id, inp.cap_presented_micro, now),
                )
                pinned = self._conn.execute(
                    "SELECT cap_micro FROM job_caps WHERE caller_id = ? AND job_id = ?",
                    (inp.caller_id, inp.job_id),
                ).fetchone()
                if int(pinned["cap_micro"]) != inp.cap_presented_micro:
                    raise JobCapMismatch(int(pinned["cap_micro"]))
            else:
                for basis, would in (("billed", inp.reserved_micro), ("list", inp.list_reserved_micro)):
                    if would > inp.cap_presented_micro:
                        raise BudgetRefusal(
                            "request",
                            basis,
                            inp.cap_presented_micro,
                            inp.cap_presented_micro,
                            would,
                            self._affordable(inp, basis, inp.cap_presented_micro),
                        )

            scopes = scopes_for(inp.caller_id, inp.job_id, inp.project_id, period, day, inp.lane_used)
            for scope in scopes:
                for basis in BASES:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO scope_totals "
                        "(scope_kind, scope_id, period_key, basis, held_micro, settled_micro) "
                        "VALUES (?, ?, ?, ?, 0, 0)",
                        (scope.kind, scope.id, scope.period_key, basis),
                    )

            # Enforced (scope, basis) pairs, tightest-wins order (spec §5).
            enforced: list[tuple[Scope, str, int, str]] = []
            if inp.job_id is not None:
                job_scope = Scope("job", f"{inp.caller_id}/{inp.job_id}", "")
                enforced.append((job_scope, "billed", inp.cap_presented_micro, "job"))
                enforced.append((job_scope, "list", inp.cap_presented_micro, "job"))
            if inp.caller_day_cap_micro is not None:
                enforced.append(
                    (
                        Scope("caller_day", inp.caller_id, day),
                        "billed",
                        inp.caller_day_cap_micro,
                        "caller_day",
                    )
                )
            enforced.append(
                (Scope("project", inp.project_id, period), "billed", inp.project_cap_micro, "project_period")
            )
            enforced.append((Scope("brake", inp.lane_used, day), "billed", inp.brake_cap_micro, brake_scope_name))
            enforced_keys = {(scope, basis) for scope, basis, _, _ in enforced}

            for scope, basis, cap, name in enforced:
                would = inp.reserved_micro if basis == "billed" else inp.list_reserved_micro
                cursor = self._conn.execute(
                    "UPDATE scope_totals SET held_micro = held_micro + ? "
                    "WHERE scope_kind = ? AND scope_id = ? AND period_key = ? AND basis = ? "
                    "AND held_micro + settled_micro + ? <= ?",
                    (would, scope.kind, scope.id, scope.period_key, basis, would, cap),
                )
                if cursor.rowcount != 1:
                    totals = self._totals_locked(scope)
                    remaining = max(0, cap - totals.committed(basis))
                    raise BudgetRefusal(name, basis, cap, remaining, would, self._affordable(inp, basis, remaining))

            # Unenforced pairs are still materialized (reads and recompute).
            for scope in scopes:
                for basis in BASES:
                    if (scope, basis) in enforced_keys:
                        continue
                    would = inp.reserved_micro if basis == "billed" else inp.list_reserved_micro
                    self._conn.execute(
                        "UPDATE scope_totals SET held_micro = held_micro + ? "
                        "WHERE scope_kind = ? AND scope_id = ? AND period_key = ? AND basis = ?",
                        (would, scope.kind, scope.id, scope.period_key, basis),
                    )

            request_id = self._new_id()
            self._conn.execute(
                "INSERT INTO requests (id, caller_id, class, project_id, job_id, period, day, lane_requested, "
                "lane_used, fallback, model, state, cap_presented_micro, reserved_micro, list_reserved_micro, "
                "max_tokens, upstream_started, reserved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, 0, ?)",
                (
                    request_id,
                    inp.caller_id,
                    inp.caller_class,
                    inp.project_id,
                    inp.job_id,
                    period,
                    day,
                    inp.lane_requested,
                    inp.lane_used,
                    1 if inp.fallback else 0,
                    inp.model,
                    inp.cap_presented_micro,
                    inp.reserved_micro,
                    inp.list_reserved_micro,
                    inp.max_tokens,
                    now,
                ),
            )
            self._touch_commit_ts(now)
        return Reservation(
            id=request_id,
            period=period,
            day=day,
            reserved_at=now,
            reserved_micro=inp.reserved_micro,
            list_reserved_micro=inp.list_reserved_micro,
        )

    def _trip_brake_locked(self, lane: str, reason: str) -> None:
        now = self._now()
        with suppress(sqlite3.Error), self._tx():
            self._conn.execute(
                "INSERT INTO brakes (lane, tripped_at, reason) VALUES (?, ?, ?) "
                "ON CONFLICT(lane) DO UPDATE SET tripped_at = excluded.tripped_at, reason = excluded.reason, "
                "reset_at = NULL",
                (lane, now, reason),
            )
        self._log("brake_tripped", lane=lane, reason=reason)

    # ----------------------------------------------------------------- transitions

    def mark_upstream_started(self, request_id: str) -> bool:
        """``message_start`` seen: from here the row can never be released."""
        with self._lock:
            try:
                with self._tx():
                    cursor = self._conn.execute(
                        "UPDATE requests SET upstream_started = 1 WHERE id = ? AND state = 'reserved'",
                        (request_id,),
                    )
                    return cursor.rowcount == 1
            except sqlite3.Error as err:
                raise LedgerUnavailable(str(err)) from err

    def settle(
        self,
        request_id: str,
        *,
        usage: TokenUsage,
        settled_micro: int,
        list_micro: int,
        model_used: str | None,
        provider_request_id: str | None,
        http_status: int,
        latency_ms: int,
        inference_geo: str | None,
    ) -> SettleResult:
        with self._lock:
            try:
                with self._tx():
                    row = self._reserved_row(request_id)
                    if row is None:
                        self._log("settle_ignored", request_id=request_id, reason="row not reserved")
                        return SettleResult(False, "", 0, 0, False)
                    now = self._now()
                    self._conn.execute(
                        "UPDATE requests SET state = 'settled', settled_micro = ?, list_micro = ?, input_tokens = ?, "
                        "output_tokens = ?, cache_write_5m_tokens = ?, cache_write_1h_tokens = ?, "
                        "cache_read_tokens = ?, "
                        "inference_geo = ?, model_used = ?, provider_request_id = ?, http_status = ?, latency_ms = ?, "
                        "settled_at = ? WHERE id = ? AND state = 'reserved'",
                        (
                            settled_micro,
                            list_micro,
                            usage.input_tokens,
                            usage.output_tokens,
                            usage.cache_write_5m_tokens,
                            usage.cache_write_1h_tokens,
                            usage.cache_read_tokens,
                            inference_geo,
                            model_used,
                            provider_request_id,
                            http_status,
                            latency_ms,
                            now,
                            request_id,
                        ),
                    )
                    self._move_totals(row, settled_micro, list_micro)
                    self._touch_commit_ts(now)
                    over = settled_micro > row["reserved_micro"] or list_micro > row["list_reserved_micro"]
                    return SettleResult(True, "settled", settled_micro, list_micro, over)
            except sqlite3.Error as err:
                raise LedgerUnavailable(str(err)) from err

    def finish_without_usage(
        self,
        request_id: str,
        outcome: FinishOutcome,
        *,
        error_code: str,
        http_status: int,
        latency_ms: int,
    ) -> SettleResult:
        """Release (no generation proven) or settle at the reservation (timeout / aborted)."""
        state = {"release": "released", "timeout": "timeout", "aborted": "aborted"}[outcome]
        with self._lock:
            try:
                with self._tx():
                    row = self._reserved_row(request_id)
                    if row is None:
                        self._log(
                            "finish_ignored",
                            request_id=request_id,
                            outcome=outcome,
                            reason="row not reserved",
                        )
                        return SettleResult(False, "", 0, 0, False)
                    if state == "released" and int(row["upstream_started"]) == 1:
                        # Never release after message_start: unknown spend settles at the reservation.
                        self._log("release_refused_after_generation", request_id=request_id)
                        state = "aborted"
                    settled = 0 if state == "released" else int(row["reserved_micro"])
                    listed = 0 if state == "released" else int(row["list_reserved_micro"])
                    now = self._now()
                    self._conn.execute(
                        "UPDATE requests SET state = ?, settled_micro = ?, list_micro = ?, error_code = ?, "
                        "http_status = ?, latency_ms = ?, settled_at = ? WHERE id = ? AND state = 'reserved'",
                        (state, settled, listed, error_code, http_status, latency_ms, now, request_id),
                    )
                    self._move_totals(row, settled, listed)
                    self._touch_commit_ts(now)
                    return SettleResult(True, state, settled, listed, False)
            except sqlite3.Error as err:
                raise LedgerUnavailable(str(err)) from err

    # ----------------------------------------------------------------- boot

    def sweep(self, boot_ts: int) -> SweepResult:
        """Every ``reserved`` row older than boot is an orphan: settle it at the reservation."""
        by_project: dict[str, int] = defaultdict(int)
        count = 0
        with self._lock, self._tx():
            rows = self._conn.execute(
                "SELECT id, caller_id, job_id, project_id, period, day, lane_used, state, reserved_micro, "
                "list_reserved_micro, upstream_started, reserved_at FROM requests "
                "WHERE state = 'reserved' AND reserved_at < ? ORDER BY reserved_at",
                (boot_ts,),
            ).fetchall()
            now = self._now()
            for row in rows:
                self._conn.execute(
                    "UPDATE requests SET state = 'swept', settled_micro = reserved_micro, "
                    "list_micro = list_reserved_micro, "
                    "error_code = 'E_SWEPT', settled_at = ? WHERE id = ? AND state = 'reserved'",
                    (now, row["id"]),
                )
                self._move_totals(row, int(row["reserved_micro"]), int(row["list_reserved_micro"]))
                by_project[str(row["project_id"])] += int(row["reserved_micro"])
                count += 1
                self._log(
                    "sweep",
                    request_id=row["id"],
                    project=row["project_id"],
                    caller_id=row["caller_id"],
                    reserved_usd=micro_to_usd_str(int(row["reserved_micro"])),
                    upstream_started=int(row["upstream_started"]),
                )
            if rows:
                self._touch_commit_ts(now)
        return SweepResult(count, dict(by_project))

    def quick_check(self) -> bool:
        with self._lock:
            row = self._conn.execute("PRAGMA quick_check").fetchone()
            return row is not None and str(row[0]) == "ok"

    def recompute_totals(self) -> list[str]:
        """Rebuild every scope total from ``requests`` and diff against ``scope_totals``.

        Returns the mismatches (empty = consistent). The stored table is left
        untouched on mismatch: readiness goes false and a human looks at the
        evidence (spec §1).
        """
        expected: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0])
        with self._lock:
            for row in self._conn.execute(
                "SELECT caller_id, job_id, project_id, period, day, lane_used, state, reserved_micro, "
                "list_reserved_micro, settled_micro, list_micro FROM requests"
            ):
                for scope in self._row_scopes(row):
                    for basis in BASES:
                        key = (scope.kind, scope.id, scope.period_key, basis)
                        reserved = int(row["reserved_micro"] if basis == "billed" else row["list_reserved_micro"])
                        if row["state"] == "reserved":
                            expected[key][0] += reserved
                        elif row["state"] in CHARGED_STATES:
                            charged = row["settled_micro"] if basis == "billed" else row["list_micro"]
                            expected[key][1] += int(charged or 0)
            stored: dict[tuple[str, str, str, str], tuple[int, int]] = {}
            for row in self._conn.execute(
                "SELECT scope_kind, scope_id, period_key, basis, held_micro, settled_micro FROM scope_totals"
            ):
                stored[(row["scope_kind"], row["scope_id"], row["period_key"], row["basis"])] = (
                    int(row["held_micro"]),
                    int(row["settled_micro"]),
                )
        mismatches: list[str] = []
        for key in sorted(set(expected) | set(stored)):
            want = tuple(expected.get(key, [0, 0]))
            have = stored.get(key, (0, 0))
            if want != have:
                mismatches.append(f"{key}: stored held/settled {have} != recomputed {want}")
        return mismatches

    # ----------------------------------------------------------------- probes

    def read_ping(self) -> None:
        """Liveness: never a write."""
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def write_ping(self) -> None:
        """Readiness: a real durable write — a full disk must take the gateway down (spec §8)."""
        with self._lock:
            try:
                with self._tx():
                    self._meta_set("ready_ping", str(self._now()))
            except sqlite3.Error as err:
                raise LedgerUnavailable(str(err)) from err

    def clock_ok(self, now: int) -> bool:
        with self._lock:
            last = self._last_commit_ts()
        return last is None or now >= last - CLOCK_SKEW_MS

    # ----------------------------------------------------------------- reads

    def totals(self, scope: Scope) -> Totals:
        with self._lock:
            return self._totals_locked(scope)

    def project_totals(self, project_id: str, period: str) -> Totals:
        return self.totals(Scope("project", project_id, period))

    def job_totals(self, caller_id: str, job_id: str) -> Totals:
        return self.totals(Scope("job", f"{caller_id}/{job_id}", ""))

    def job_cap(self, caller_id: str, job_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cap_micro FROM job_caps WHERE caller_id = ? AND job_id = ?", (caller_id, job_id)
            ).fetchone()
        return None if row is None else int(row["cap_micro"])

    def job_request_count(self, caller_id: str, job_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE caller_id = ? AND job_id = ?", (caller_id, job_id)
            ).fetchone()
        return int(row["n"])

    def request_row(self, request_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return None if row is None else dict(row)

    def requests_for_project(self, project_id: str, since_ms: int, limit: int = 500) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests WHERE project_id = ? AND reserved_at >= ? "
                "ORDER BY reserved_at DESC, id DESC LIMIT ?",
                (project_id, since_ms, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def in_flight(self) -> tuple[int, int | None]:
        """(count of reserved rows, oldest reserved_at)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(reserved_at) AS oldest FROM requests WHERE state = 'reserved'"
            ).fetchone()
        return int(row["n"]), (None if row["oldest"] is None else int(row["oldest"]))

    def brake_state(self, lane: str) -> BrakeState:
        with self._lock:
            row = self._conn.execute(
                "SELECT tripped_at, reason, reset_at, reset_by FROM brakes WHERE lane = ?", (lane,)
            ).fetchone()
            tripped = self._brake_tripped_locked(lane, self._now())
        if row is None:
            return BrakeState(lane, False, None, None, None, None)
        return BrakeState(lane, tripped, row["tripped_at"], row["reason"], row["reset_at"], row["reset_by"])

    def reset_brake(self, lane: str, *, reason: str, reset_by: str) -> BrakeState:
        """Audited (spec §3): the reset itself is a row, and the JSON log carries who and why."""
        now = self._now()
        with self._lock:
            try:
                with self._tx():
                    self._conn.execute(
                        "INSERT INTO brakes (lane, reason, reset_at, reset_by) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(lane) DO UPDATE SET reason = excluded.reason, reset_at = excluded.reset_at, "
                        "reset_by = excluded.reset_by",
                        (lane, f"reset: {reason}", now, reset_by),
                    )
            except sqlite3.Error as err:
                raise LedgerUnavailable(str(err)) from err
        self._log("brake_reset", lane=lane, reason=reason, reset_by=reset_by)
        return self.brake_state(lane)

    def lane_states(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM lane_state ORDER BY lane").fetchall()
        return [dict(row) for row in rows]

    def set_lane_state(
        self, lane: str, *, last_heartbeat: int | None, cooling_until: int | None, auth_ok: bool
    ) -> None:
        with self._lock, suppress(sqlite3.Error), self._tx():
            self._conn.execute(
                "INSERT INTO lane_state (lane, last_heartbeat, cooling_until, auth_ok) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(lane) DO UPDATE SET last_heartbeat = excluded.last_heartbeat, "
                "cooling_until = excluded.cooling_until, auth_ok = excluded.auth_ok",
                (lane, last_heartbeat, cooling_until, 1 if auth_ok else 0),
            )

    def meta_get(self, key: str) -> str | None:
        with self._lock:
            return self._meta_get(key)

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._tx():
            self._meta_set(key, value)

    def db_bytes(self) -> int:
        with self._lock:
            pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        return int(pages) * int(size)
