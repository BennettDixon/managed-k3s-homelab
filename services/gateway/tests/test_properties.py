"""Spec §14 property tests for the money invariants.

* after every commit, for every enforced scope and basis, Σ settled + Σ held <= cap;
* N parallel reserves of worst case w against cap = k·w admit exactly k;
* for a fault injected between any two statements, after restart no row is
  reserved and every swept row has settled = reserved (and the totals recompute);
* the stored totals always recompute exactly from the request rows.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, multiple, rule

from gateway.db import open_db
from gateway.jsonlog import null_log
from gateway.ledger import BudgetRefusal, JobCapMismatch, Ledger
from gateway.money import TokenUsage
from tests.conftest import FakeClock, make_reserve_input

PROJECT_CAPS = {"homelab-ops": 5_000, "gateway-smoke": 3_000}
CALLER_DAY_CAP = 4_000
BRAKE_CAP = 6_000


def enforced_cap(kind: str, scope_id: str, basis: str, job_caps: dict[str, int]) -> int | None:
    if kind == "project" and basis == "billed":
        return PROJECT_CAPS[scope_id]
    if kind == "caller_day" and basis == "billed":
        return CALLER_DAY_CAP
    if kind == "brake" and basis == "billed":
        return BRAKE_CAP
    if kind == "job":
        return job_caps[scope_id.split("/", 1)[1]]
    return None


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.clock = FakeClock()
        self.ledger = Ledger(open_db(":memory:"), now_ms=self.clock.now_ms, log=null_log)
        self.job_caps: dict[str, int] = {}
        self.reserved: dict[str, int] = {}
        self.started: set[str] = set()

    reservations: Bundle[str] = Bundle("reservations")

    @rule(
        target=reservations,
        project=st.sampled_from(sorted(PROJECT_CAPS)),
        job=st.one_of(st.none(), st.sampled_from(["j1", "j2"])),
        in_tokens=st.integers(min_value=0, max_value=300),
        max_tokens=st.integers(min_value=1, max_value=300),
        cap=st.integers(min_value=0, max_value=3_000),
    )
    def reserve(self, project: str, job: str | None, in_tokens: int, max_tokens: int, cap: int) -> Any:
        if job is not None:
            cap = self.job_caps.setdefault(job, cap)
        inp = make_reserve_input(
            project_id=project,
            job_id=job,
            in_tokens=in_tokens,
            max_tokens=max_tokens,
            cap_presented_micro=cap,
            project_cap_micro=PROJECT_CAPS[project],
            caller_day_cap_micro=CALLER_DAY_CAP,
            brake_cap_micro=BRAKE_CAP,
        )
        try:
            res = self.ledger.reserve(inp)
        except BudgetRefusal:
            return multiple()
        except JobCapMismatch:
            raise AssertionError("pin drift") from None
        self.reserved[res.id] = res.reserved_micro
        return res.id

    @rule(rid=consumes(reservations), fraction=st.integers(min_value=0, max_value=100))
    def settle(self, rid: str, fraction: int) -> None:
        reserved = self.reserved.pop(rid)
        amount = reserved * fraction // 100
        res = self.ledger.settle(
            rid,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            settled_micro=amount,
            list_micro=amount,
            model_used="m",
            provider_request_id="r",
            http_status=200,
            latency_ms=1,
            inference_geo=None,
        )
        assert res.applied and res.state == "settled" and not res.over_reserve

    @rule(rid=consumes(reservations), outcome=st.sampled_from(["release", "timeout", "aborted"]))
    def finish(self, rid: str, outcome: str) -> None:
        reserved = self.reserved.pop(rid)
        res = self.ledger.finish_without_usage(rid, outcome, error_code="E", http_status=502, latency_ms=1)  # type: ignore[arg-type]
        assert res.applied
        if outcome == "release" and rid in self.started:
            # Never release after message_start: the ledger converts it to aborted at the reservation.
            assert res.state == "aborted" and res.settled_micro == reserved
        else:
            assert res.settled_micro == (0 if outcome == "release" else reserved)

    @rule(rid=reservations)
    def mark_started(self, rid: str) -> None:
        assert self.ledger.mark_upstream_started(rid) is True
        self.started.add(rid)

    @rule(ms=st.integers(min_value=0, max_value=6 * 3_600_000))
    def advance_clock(self, ms: int) -> None:
        self.clock.advance(ms)

    @rule()
    def reset_brake(self) -> None:
        self.ledger.reset_brake("metered", reason="test", reset_by="operator")

    @invariant()
    def caps_hold_and_totals_recompute(self) -> None:
        conn: sqlite3.Connection = self.ledger._conn
        for row in conn.execute("SELECT * FROM scope_totals"):
            assert row["held_micro"] >= 0 and row["settled_micro"] >= 0
            cap = enforced_cap(row["scope_kind"], row["scope_id"], row["basis"], self.job_caps)
            if cap is not None:
                assert row["held_micro"] + row["settled_micro"] <= cap, dict(row)
        assert self.ledger.recompute_totals() == []
        assert self.ledger.in_flight()[0] == len(self.reserved)

    @invariant()
    def no_reservation_ever_settles_above_itself_here(self) -> None:
        conn: sqlite3.Connection = self.ledger._conn
        for row in conn.execute(
            "SELECT state, reserved_micro, settled_micro FROM requests WHERE settled_micro IS NOT NULL"
        ):
            assert row["settled_micro"] <= row["reserved_micro"]


TestLedgerMachine = LedgerMachine.TestCase
TestLedgerMachine.settings = settings(
    max_examples=40, stateful_step_count=40, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


def test_parallel_reserves_admit_exactly_k(tmp_path: Path) -> None:
    ledger = Ledger(open_db(str(tmp_path / "parallel.db")), now_ms=FakeClock().now_ms, log=null_log)
    inp = make_reserve_input(job_id="par", in_tokens=100, max_tokens=100)  # w = 600
    w = inp.reserved_micro
    k, n = 7, 40
    inp = make_reserve_input(
        job_id="par",
        in_tokens=100,
        max_tokens=100,
        cap_presented_micro=k * w,
        project_cap_micro=10**9,
        brake_cap_micro=10**9,
    )

    def attempt(_: int) -> str:
        try:
            ledger.reserve(inp)
        except BudgetRefusal:
            return "refused"
        return "ok"

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(n)))
    assert results.count("ok") == k
    assert results.count("refused") == n - k
    totals = ledger.job_totals("operator", "par")
    assert totals.held_billed == k * w and totals.held_list == k * w
    assert ledger.recompute_totals() == []


class SimulatedCrash(Exception):
    pass


class CrashingConnection:
    """Raises on the Nth statement and every one after it — the process died there."""

    def __init__(self, conn: sqlite3.Connection, crash_after: int) -> None:
        self._conn = conn
        self._count = 0
        self.crash_after = crash_after
        self.crashed = False

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        if self.crashed:
            raise SimulatedCrash
        self._count += 1
        if self._count > self.crash_after:
            self.crashed = True
            raise SimulatedCrash
        return self._conn.execute(sql, params)

    @property
    def statements(self) -> int:
        return self._count


def run_scenario(ledger: Ledger, expected: dict[str, str]) -> None:
    """A fixed money workload; ``expected`` records what each returned call committed."""
    r1 = ledger.reserve(make_reserve_input())
    expected[r1.id] = "reserved"
    r2 = ledger.reserve(make_reserve_input(job_id="jx", cap_presented_micro=5_000))
    expected[r2.id] = "reserved"
    ledger.mark_upstream_started(r2.id)
    ledger.settle(
        r1.id,
        usage=TokenUsage(input_tokens=5, output_tokens=5),
        settled_micro=60,
        list_micro=60,
        model_used="m",
        provider_request_id="r",
        http_status=200,
        latency_ms=1,
        inference_geo=None,
    )
    expected[r1.id] = "settled"
    r3 = ledger.reserve(make_reserve_input())
    expected[r3.id] = "reserved"
    ledger.finish_without_usage(r3.id, "release", error_code="E", http_status=502, latency_ms=1)
    expected[r3.id] = "released"
    r4 = ledger.reserve(make_reserve_input(job_id="jx", cap_presented_micro=5_000))
    expected[r4.id] = "reserved"
    ledger.finish_without_usage(r4.id, "timeout", error_code="E_TIMEOUT", http_status=504, latency_ms=1)
    expected[r4.id] = "timeout"
    try:
        ledger.reserve(make_reserve_input(cap_presented_micro=0))
    except BudgetRefusal:
        pass
    r5 = ledger.reserve(make_reserve_input())
    expected[r5.id] = "reserved"


def test_crash_between_any_two_statements_leaves_no_reserved_row_after_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    # Count the statements of a clean run first so every gap gets a crash.
    probe_path = str(tmp_path / "probe.db")
    probe = CrashingConnection(open_db(probe_path), crash_after=10**9)
    run_scenario(Ledger(cast(sqlite3.Connection, probe), now_ms=clock.now_ms, log=null_log), {})
    total = probe.statements
    assert total > 40

    for crash_after in range(1, total + 1):
        path = str(tmp_path / f"crash-{crash_after}.db")
        raw = open_db(path)
        proxy = CrashingConnection(raw, crash_after=crash_after)
        ledger = Ledger(cast(sqlite3.Connection, proxy), now_ms=clock.now_ms, log=null_log)
        expected: dict[str, str] = {}
        try:
            run_scenario(ledger, expected)
        except SimulatedCrash:
            pass
        raw.close()  # the process died: an open transaction is rolled back by SQLite

        clock.advance(1_000)
        reopened = Ledger(open_db(path), now_ms=clock.now_ms, log=null_log)
        swept = reopened.sweep(clock.now_ms())
        assert reopened.in_flight() == (0, None), crash_after
        assert reopened.recompute_totals() == [], crash_after
        assert reopened.quick_check()
        conn: sqlite3.Connection = reopened._conn
        rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM requests")}
        for rid, row in rows.items():
            if row["state"] == "swept":
                assert row["settled_micro"] == row["reserved_micro"], crash_after
                assert row["list_micro"] == row["list_reserved_micro"], crash_after
            if row["state"] == "released":
                assert row["upstream_started"] == 0 and row["settled_micro"] == 0
            want = expected.get(rid)
            if want is not None:
                assert row["state"] == ("swept" if want == "reserved" else want), (crash_after, rid, want, row["state"])
        # Every row that a call reported as committed survived the crash.
        for rid, want in expected.items():
            assert rid in rows, (crash_after, rid, want)
        # A reserve that returned had committed (COMMIT is its last statement), so
        # the rows a call reported as reserved are exactly the rows the sweep finds.
        assert swept.count == sum(1 for want in expected.values() if want == "reserved"), crash_after
