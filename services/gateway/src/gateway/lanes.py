"""Metered lane state machine (spec §6.1): up / down with backoff / auth failed / spend-limited."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from gateway.jsonlog import Log
from gateway.money import ceil_div

CONSECUTIVE_FAILURES = 3
FAILURE_WINDOW_MS = 5 * 60_000
BACKOFF_BASE_MS = 2_000
BACKOFF_CAP_MS = 120_000
SPEND_LIMIT_REPROBE_MS = 60 * 60_000
PROBE_FAIL_DOWN_MS = 60_000


@dataclass(frozen=True)
class LaneStatus:
    lane: str
    up: bool
    auth_ok: bool
    cooling_until: int | None
    down_until: int | None
    last_ok: int | None


class MeteredLane:
    def __init__(
        self,
        *,
        now_ms: Callable[[], int],
        log: Log,
        persist: Callable[[LaneStatus], None] | None = None,
        lane: str = "metered",
    ) -> None:
        self._now = now_ms
        self._log = log
        self._persist = persist
        self.lane = lane
        self._up = True
        self._auth_ok = True
        self._cooling_until: int | None = None
        self._down_until: int | None = None
        self._last_ok: int | None = None
        self._failures: deque[int] = deque()
        self._episodes = 0

    def status(self) -> LaneStatus:
        return LaneStatus(
            lane=self.lane,
            up=self._up,
            auth_ok=self._auth_ok,
            cooling_until=self._cooling_until,
            down_until=self._down_until,
            last_ok=self._last_ok,
        )

    def _changed(self, evt: str, **fields: object) -> None:
        self._log(evt, lane=self.lane, **fields)
        if self._persist is not None:
            self._persist(self.status())

    def check(self, now: int) -> tuple[bool, int]:
        """(eligible, retry_after_s). Past ``down_until`` the lane is half-open: a request may try."""
        if not self._auth_ok:
            return False, 60
        if self._cooling_until is not None:
            if now < self._cooling_until:
                return False, max(1, ceil_div(self._cooling_until - now, 1000))
            return True, 0  # resume time passed: half-open
        if not self._up:
            if self._down_until is not None and now >= self._down_until:
                return True, 0  # half-open
            remaining = (self._down_until - now) if self._down_until is not None else 60_000
            return False, max(1, ceil_div(remaining, 1000))
        return True, 0

    def record_success(self, now: int) -> None:
        was_up = self._up and self._auth_ok and self._cooling_until is None
        self._up = True
        self._auth_ok = True
        self._cooling_until = None
        self._down_until = None
        self._last_ok = now
        self._failures.clear()
        self._episodes = 0
        if not was_up:
            self._changed("lane_up")

    def record_transport_failure(self, now: int) -> bool:
        """5xx / 529 / network before generation. Returns True when the lane just went down."""
        self._failures.append(now)
        while self._failures and self._failures[0] < now - FAILURE_WINDOW_MS:
            self._failures.popleft()
        if len(self._failures) < CONSECUTIVE_FAILURES:
            return False
        self._failures.clear()
        self._episodes += 1
        backoff = min(BACKOFF_BASE_MS * (2 ** (self._episodes - 1)), BACKOFF_CAP_MS)
        self._up = False
        self._down_until = now + backoff
        self._changed("lane_down", reason="consecutive upstream failures", backoff_ms=backoff)
        return True

    def record_auth_failure(self, now: int) -> None:
        self._auth_ok = False
        self._up = False
        self._down_until = None
        self._changed("lane_down", reason="auth")

    def record_spend_limit(self, now: int, resume_at: int | None) -> None:
        self._cooling_until = resume_at if resume_at is not None else now + SPEND_LIMIT_REPROBE_MS
        self._up = False
        self._changed("lane_down", reason="spend limit", cooling_until=self._cooling_until)

    def probe_ok(self, now: int) -> None:
        self.record_success(now)

    def probe_failed(self, now: int, kind: str) -> None:
        if kind == "auth":
            self.record_auth_failure(now)
            return
        if kind in ("rate_limited",):
            return  # a rate-limited probe says nothing about the lane
        if self._up:
            self._up = False
            self._down_until = now + PROBE_FAIL_DOWN_MS
            self._changed("lane_down", reason=f"probe {kind}")
        elif self._down_until is not None and now >= self._down_until:
            self._down_until = now + PROBE_FAIL_DOWN_MS
