from gateway.lanes import LaneStatus, MeteredLane
from tests.conftest import LogCapture


def make_lane(persisted: list[LaneStatus] | None = None) -> MeteredLane:
    return MeteredLane(now_ms=lambda: 0, log=LogCapture(), persist=persisted.append if persisted is not None else None)


def test_three_failures_in_window_take_the_lane_down_with_doubling_backoff() -> None:
    persisted: list[LaneStatus] = []
    lane = make_lane(persisted)
    assert lane.check(0) == (True, 0)
    assert lane.record_transport_failure(1_000) is False
    assert lane.record_transport_failure(2_000) is False
    assert lane.record_transport_failure(3_000) is True
    assert lane.check(3_000) == (False, 2)
    assert lane.check(4_999) == (False, 1)
    assert lane.check(5_000) == (True, 0)  # half-open after the 2 s backoff
    first_down = persisted[-1]
    assert not first_down.up and first_down.down_until == 5_000
    for t in (6_000, 6_001, 6_002):
        lane.record_transport_failure(t)
    assert lane.status().down_until == 6_002 + 4_000  # second episode doubles
    lane.record_success(10_100)
    after_success = persisted[-1]
    assert lane.check(10_100) == (True, 0) and after_success.up
    for t in (11_000, 11_001, 11_002):
        lane.record_transport_failure(t)
    assert lane.status().down_until == 11_002 + 2_000  # episodes reset on success


def test_backoff_is_capped_at_two_minutes() -> None:
    lane = make_lane()
    t = 0
    for _ in range(10):
        for _ in range(3):
            lane.record_transport_failure(t)
        t += 1
    down_until = lane.status().down_until
    assert down_until is not None
    assert down_until - (t - 1) == 120_000


def test_failures_outside_the_five_minute_window_do_not_accumulate() -> None:
    lane = make_lane()
    assert lane.record_transport_failure(0) is False
    assert lane.record_transport_failure(1_000) is False
    assert lane.record_transport_failure(400_000) is False  # the first two aged out
    assert lane.check(400_000) == (True, 0)


def test_auth_failure_and_spend_limit() -> None:
    persisted: list[LaneStatus] = []
    lane = make_lane(persisted)
    lane.record_auth_failure(10)
    assert lane.check(10) == (False, 60)
    assert persisted[-1].auth_ok is False
    lane.record_success(20)
    assert lane.check(20) == (True, 0) and lane.status().auth_ok is True

    lane.record_spend_limit(1_000, resume_at=61_000)
    assert lane.check(1_000) == (False, 60)
    assert lane.check(61_000) == (True, 0)  # resume time passed: half-open
    lane.record_spend_limit(2_000, resume_at=None)
    assert lane.status().cooling_until == 2_000 + 3_600_000


def test_probe_never_clears_a_spend_limit_cooldown() -> None:
    # Slice-2 review: models.list() is not gated by a workspace spend limit, so
    # a passing probe must not flap the lane up (that would silence
    # GatewayLaneDown and make the runbook's $1 trip unprovable).
    lane = make_lane()
    lane.record_spend_limit(1_000, resume_at=61_000)
    lane.probe_ok(2_000)
    assert lane.status().up is False
    assert lane.status().cooling_until == 61_000
    assert lane.status().last_ok == 2_000
    assert lane.check(2_000) == (False, 59)
    # A rotated key during the cooldown: the probe still proves auth.
    lane.record_auth_failure(3_000)
    lane.probe_ok(4_000)
    assert lane.status().auth_ok is True and lane.status().up is False
    # Past the resume time the lane is half-open; only a real success clears cooling.
    assert lane.check(61_000) == (True, 0)
    lane.probe_ok(61_500)
    assert lane.status().cooling_until == 61_000 and lane.status().up is False
    lane.record_success(62_000)
    assert lane.status().up is True and lane.status().cooling_until is None


def test_probe_transitions() -> None:
    lane = make_lane()
    lane.probe_failed(0, "rate_limited")
    assert lane.check(0) == (True, 0)  # a rate-limited probe says nothing
    lane.probe_failed(0, "network")
    assert lane.check(0) == (False, 60)
    assert lane.check(60_000) == (True, 0)
    lane.probe_failed(60_000, "server")  # still failing at the half-open point: another 60 s
    assert lane.check(60_000) == (False, 60)
    lane.probe_failed(70_000, "auth")
    assert lane.status().auth_ok is False
    lane.probe_ok(80_000)
    assert lane.check(80_000) == (True, 0) and lane.status().last_ok == 80_000
