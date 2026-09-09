"""Ledger transitions (spec §1) against an in-memory DB with a controllable clock."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gateway.db import open_db
from gateway.ledger import (
    CHARGED_STATES,
    BudgetRefusal,
    ClockGuardTripped,
    JobCapMismatch,
    Ledger,
    LedgerUnavailable,
    Scope,
    period_day,
)
from gateway.money import TokenUsage
from tests.conftest import USD, FakeClock, LogCapture, make_reserve_input


def dump(ledger: Ledger) -> str:
    conn: sqlite3.Connection = ledger._conn
    return "\n".join(conn.iterdump())


def ts(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


def settle(ledger: Ledger, request_id: str, *, settled: int = 60, listed: int | None = None) -> None:
    res = ledger.settle(
        request_id,
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        settled_micro=settled,
        list_micro=listed if listed is not None else settled,
        model_used="claude-haiku-4-5",
        provider_request_id="req_1",
        http_status=200,
        latency_ms=5,
        inference_geo=None,
    )
    assert res.applied


def test_reserve_then_settle_moves_held_to_settled(ledger: Ledger, clock: FakeClock) -> None:
    inp = make_reserve_input()  # worst case: 100 in + 100 out on haiku = 100 + 500 micro
    res = ledger.reserve(inp)
    assert res.reserved_micro == 600 and res.list_reserved_micro == 600
    period, day = period_day(clock.now_ms())
    assert (res.period, res.day) == (period, day)
    project = ledger.project_totals("homelab-ops", period)
    assert (project.held_billed, project.settled_billed) == (600, 0)
    row = ledger.request_row(res.id)
    assert row is not None and row["state"] == "reserved" and row["upstream_started"] == 0

    settle(ledger, res.id, settled=60)
    row = ledger.request_row(res.id)
    assert row is not None
    assert row["state"] == "settled" and row["settled_micro"] == 60 and row["list_micro"] == 60
    assert row["input_tokens"] == 10 and row["provider_request_id"] == "req_1"
    project = ledger.project_totals("homelab-ops", period)
    assert (project.held_billed, project.settled_billed) == (0, 60)
    brake = ledger.totals(Scope("brake", "metered", day))
    assert (brake.held_billed, brake.settled_billed) == (0, 60)
    caller_day = ledger.totals(Scope("caller_day", "operator", day))
    assert caller_day.settled_billed == 60
    assert ledger.recompute_totals() == []


def test_settle_twice_is_a_logged_no_op_and_byte_identical(ledger: Ledger, log: LogCapture) -> None:
    res = ledger.reserve(make_reserve_input())
    settle(ledger, res.id, settled=60)
    before = dump(ledger)
    again = ledger.settle(
        res.id,
        usage=TokenUsage(input_tokens=999, output_tokens=999),
        settled_micro=999_999,
        list_micro=999_999,
        model_used="x",
        provider_request_id="dup",
        http_status=200,
        latency_ms=1,
        inference_geo=None,
    )
    assert again.applied is False
    assert dump(ledger) == before
    assert log.events("settle_ignored")


def test_release_only_before_generation(ledger: Ledger) -> None:
    a = ledger.reserve(make_reserve_input())
    released = ledger.finish_without_usage(
        a.id, "release", error_code="E_UPSTREAM_ERROR", http_status=502, latency_ms=1
    )
    assert released.state == "released" and released.settled_micro == 0
    row = ledger.request_row(a.id)
    assert row is not None and row["state"] == "released" and row["settled_micro"] == 0 and row["list_micro"] == 0

    b = ledger.reserve(make_reserve_input())
    assert ledger.mark_upstream_started(b.id) is True
    refused = ledger.finish_without_usage(b.id, "release", error_code="E_UPSTREAM_ERROR", http_status=502, latency_ms=1)
    # Never release after message_start: unknown spend settles at the reservation.
    assert refused.state == "aborted" and refused.settled_micro == b.reserved_micro
    period, _ = period_day(ledger._now())
    totals = ledger.project_totals("homelab-ops", period)
    assert totals.held_billed == 0 and totals.settled_billed == b.reserved_micro
    assert ledger.recompute_totals() == []


def test_timeout_and_abort_settle_at_the_reservation(ledger: Ledger) -> None:
    for outcome, state in (("timeout", "timeout"), ("aborted", "aborted")):
        res = ledger.reserve(make_reserve_input())
        fin = ledger.finish_without_usage(res.id, outcome, error_code="E_TIMEOUT", http_status=504, latency_ms=1)  # type: ignore[arg-type]
        assert fin.state == state
        row = ledger.request_row(res.id)
        assert row is not None
        assert row["settled_micro"] == row["reserved_micro"] == 600
        assert row["list_micro"] == row["list_reserved_micro"] == 600
    assert ledger.recompute_totals() == []


def test_mark_started_after_finish_is_false(ledger: Ledger) -> None:
    res = ledger.reserve(make_reserve_input())
    ledger.finish_without_usage(res.id, "release", error_code="x", http_status=502, latency_ms=1)
    assert ledger.mark_upstream_started(res.id) is False
    assert (
        ledger.finish_without_usage(res.id, "timeout", error_code="x", http_status=504, latency_ms=1).applied is False
    )


def test_cap_zero_is_refused_with_no_row_and_untouched_db(ledger: Ledger) -> None:
    before = dump(ledger)
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(make_reserve_input(cap_presented_micro=0))
    assert info.value.scope == "request" and info.value.would_reserve_micro == 600
    assert info.value.affordable_max_tokens == 0
    assert dump(ledger) == before


def test_request_cap_refusal_reports_affordable_tokens(ledger: Ledger) -> None:
    # cap 400 micro: input costs 100, output price 5 micro/token -> 60 tokens fit
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(make_reserve_input(cap_presented_micro=400))
    assert info.value.scope == "request"
    assert info.value.remaining_micro == 400
    assert info.value.affordable_max_tokens == 60
    ledger.reserve(make_reserve_input(cap_presented_micro=400, max_tokens=60))


def test_job_cap_pin_and_cumulative_scope(ledger: Ledger) -> None:
    job = make_reserve_input(job_id="job-1", cap_presented_micro=1500)
    a = ledger.reserve(job)  # holds 600
    b = ledger.reserve(job)  # holds 1200
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(job)  # 1800 > 1500
    assert info.value.scope == "job" and info.value.remaining_micro == 300
    with pytest.raises(JobCapMismatch) as mismatch:
        ledger.reserve(make_reserve_input(job_id="job-1", cap_presented_micro=5000))
    assert mismatch.value.pinned_micro == 1500
    assert ledger.job_cap("operator", "job-1") == 1500
    settle(ledger, a.id, settled=10)
    ledger.finish_without_usage(b.id, "release", error_code="x", http_status=502, latency_ms=1)
    ledger.reserve(job)  # 10 settled + 600 <= 1500
    totals = ledger.job_totals("operator", "job-1")
    assert totals.settled_list == 10 and totals.held_list == 600
    assert ledger.job_request_count("operator", "job-1") == 3
    # A different caller's job with the same id is a different scope.
    ledger.reserve(make_reserve_input(caller_id="worker-x", job_id="job-1", cap_presented_micro=700))
    assert ledger.recompute_totals() == []


def test_project_period_cap_and_caller_day_cap(ledger: Ledger) -> None:
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(make_reserve_input(project_cap_micro=500))
    assert info.value.scope == "project_period" and info.value.remaining_micro == 500
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(make_reserve_input(caller_day_cap_micro=599))
    assert info.value.scope == "caller_day"
    ledger.reserve(make_reserve_input(caller_day_cap_micro=600))
    with pytest.raises(BudgetRefusal):
        ledger.reserve(make_reserve_input(caller_day_cap_micro=600))


def test_brake_trips_latches_resets_and_clears_at_midnight(ledger: Ledger, clock: FakeClock) -> None:
    clock.set(ts(2026, 9, 9, 12))
    big = make_reserve_input(in_tokens=1000, max_tokens=500_000, cap_presented_micro=5 * USD, brake_cap_micro=3 * USD)
    assert big.reserved_micro == 1000 + 2_500_000
    first = ledger.reserve(big)
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(big)
    assert info.value.scope == "brake_metered" and info.value.latched is False
    assert ledger.brake_state("metered").tripped is True
    # Latched: even a tiny request is refused until reset or midnight.
    with pytest.raises(BudgetRefusal) as info:
        ledger.reserve(make_reserve_input(brake_cap_micro=3 * USD))
    assert info.value.scope == "brake_metered" and info.value.latched is True
    settle(ledger, first.id, settled=1000)
    with pytest.raises(BudgetRefusal):
        ledger.reserve(make_reserve_input(brake_cap_micro=3 * USD))
    state = ledger.reset_brake("metered", reason="offender cancelled", reset_by="operator")
    assert state.tripped is False and state.reset_by == "operator"
    ledger.reserve(make_reserve_input(brake_cap_micro=3 * USD))
    # Trip again (the first big settled small, so one more big fits), then roll the day: the latch clears.
    ledger.reserve(big)
    with pytest.raises(BudgetRefusal):
        ledger.reserve(big)
    assert ledger.brake_state("metered").tripped is True
    clock.set(ts(2026, 9, 10, 0, 0, 1))
    assert ledger.brake_state("metered").tripped is False
    ledger.reserve(make_reserve_input(brake_cap_micro=3 * USD))
    assert ledger.recompute_totals() == []


def test_period_pinned_at_reserve_across_a_month_boundary(ledger: Ledger, clock: FakeClock) -> None:
    clock.set(ts(2026, 9, 30, 23, 59, 59))
    res = ledger.reserve(make_reserve_input())
    clock.set(ts(2026, 10, 1, 0, 0, 5))
    settle(ledger, res.id, settled=60)
    row = ledger.request_row(res.id)
    assert row is not None and row["period"] == "2026-09" and row["day"] == "2026-09-30"
    assert ledger.project_totals("homelab-ops", "2026-09").settled_billed == 60
    assert ledger.project_totals("homelab-ops", "2026-10").settled_billed == 0
    assert ledger.recompute_totals() == []


def test_clock_guard_refuses_backwards_time(ledger: Ledger, clock: FakeClock) -> None:
    clock.set(ts(2026, 9, 9, 12))
    ledger.reserve(make_reserve_input())
    clock.advance(-59_000)
    ledger.reserve(make_reserve_input())  # within skew
    clock.advance(-2_000)  # now 61 s behind the last commit
    assert ledger.clock_ok(clock.now_ms()) is False
    with pytest.raises(ClockGuardTripped):
        ledger.reserve(make_reserve_input())
    clock.advance(70_000)
    assert ledger.clock_ok(clock.now_ms()) is True
    ledger.reserve(make_reserve_input())


def test_clock_guard_refuses_an_earlier_period_even_within_skew(ledger: Ledger, clock: FakeClock) -> None:
    clock.set(ts(2026, 10, 1, 0, 0, 30))
    ledger.reserve(make_reserve_input())
    clock.set(ts(2026, 9, 30, 23, 59, 50))  # 40 s back, but the previous month
    with pytest.raises(ClockGuardTripped):
        ledger.reserve(make_reserve_input())


def test_sweep_settles_orphans_at_the_reservation(clock: FakeClock, log: LogCapture, tmp_path: Path) -> None:
    path = str(tmp_path / "l.db")
    conn = open_db(path)
    ledger = Ledger(conn, now_ms=clock.now_ms, log=log)
    a = ledger.reserve(make_reserve_input())
    b = ledger.reserve(make_reserve_input(project_id="gateway-smoke"))
    ledger.mark_upstream_started(b.id)
    c = ledger.reserve(make_reserve_input())
    settle(ledger, c.id, settled=5)
    conn.close()  # the process dies

    conn2 = open_db(path)
    ledger2 = Ledger(conn2, now_ms=clock.now_ms, log=log)
    clock.advance(1_000)
    result = ledger2.sweep(clock.now_ms())
    assert result.count == 2
    assert result.swept_billed_micro_by_project == {"homelab-ops": 600, "gateway-smoke": 600}
    for rid in (a.id, b.id):
        row = ledger2.request_row(rid)
        assert row is not None and row["state"] == "swept"
        assert row["settled_micro"] == row["reserved_micro"] and row["list_micro"] == row["list_reserved_micro"]
        assert row["error_code"] == "E_SWEPT"
    assert ledger2.in_flight() == (0, None)
    assert ledger2.recompute_totals() == []
    assert ledger2.quick_check() is True
    # A second sweep with the same boot timestamp finds nothing.
    assert ledger2.sweep(clock.now_ms()).count == 0
    # A row reserved AFTER boot is never swept by a sweep keyed on that boot.
    boot_ts = clock.now_ms()
    d = ledger2.reserve(make_reserve_input())
    assert ledger2.sweep(boot_ts).count == 0
    row = ledger2.request_row(d.id)
    assert row is not None and row["state"] == "reserved"


def test_recompute_detects_tampered_totals(ledger: Ledger) -> None:
    res = ledger.reserve(make_reserve_input())
    settle(ledger, res.id, settled=60)
    assert ledger.recompute_totals() == []
    ledger._conn.execute("UPDATE scope_totals SET settled_micro = settled_micro + 1 WHERE scope_kind = 'project'")
    mismatches = ledger.recompute_totals()
    assert len(mismatches) == 2  # both bases of the project scope
    assert "project" in mismatches[0]


def test_write_failure_is_ledger_unavailable(ledger: Ledger) -> None:
    ledger._conn.execute("DROP TABLE scope_totals")
    with pytest.raises(LedgerUnavailable):
        ledger.reserve(make_reserve_input())


def test_charged_states_are_exactly_the_spec_set() -> None:
    assert {"settled", "timeout", "aborted", "swept"} == CHARGED_STATES


def test_reads(ledger: Ledger, clock: FakeClock) -> None:
    res = ledger.reserve(make_reserve_input())
    count, oldest = ledger.in_flight()
    assert count == 1 and oldest == clock.now_ms()
    rows = ledger.requests_for_project("homelab-ops", 0)
    assert [r["id"] for r in rows] == [res.id]
    assert ledger.requests_for_project("homelab-ops", clock.now_ms() + 1) == []
    ledger.set_lane_state("metered", last_heartbeat=1, cooling_until=None, auth_ok=True)
    assert ledger.lane_states()[0]["lane"] == "metered"
    ledger.meta_set("k", "v")
    assert ledger.meta_get("k") == "v"
    assert ledger.db_bytes() > 0
    ledger.read_ping()
    ledger.write_ping()
