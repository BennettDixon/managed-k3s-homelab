"""Metrics (spec §9). Money rules use counters only, so a Recreate never zeroes a gauge mid-alert.

Gauges are sampled at scrape time from the ledger; http.py wraps the scrape
so a throwing collector degrades to a failed scrape, never a crashed process.
Known label combinations are zero-filled so a quiet gateway reads 0 rather
than making a series vanish (the absent-guard idiom).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from gateway.lanes import LaneStatus
from gateway.ledger import Ledger, period_day
from gateway.money import micro_to_usd_float

OUTCOMES: tuple[str, ...] = (
    "ok",
    "refused_cap",
    "refused_budget",
    "rejected",
    "lane_unavailable",
    "upstream_error",
    "timeout",
)
TOKEN_KINDS: tuple[str, ...] = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
REFUSAL_SCOPES: tuple[str, ...] = (
    "request",
    "job",
    "caller_day",
    "project_period",
    "window",
    "brake_metered",
    "brake_subscription",
)
UPSTREAM_KINDS: tuple[str, ...] = (
    "rate_limited",
    "spend_limit",
    "auth",
    "rejected",
    "server",
    "network",
    "timeout",
    "bad_response",
    "count_tokens",
)


class _Sampled(Collector):
    def __init__(self, sample: Callable[[], list[Metric]]) -> None:
        self._sample = sample

    def collect(self) -> Iterable[Metric]:
        return self._sample()


class Metrics:
    def __init__(
        self,
        *,
        projects: Sequence[str],
        ledger: Ledger,
        lane_status: Callable[[], LaneStatus],
        project_caps: Callable[[], dict[str, int]],
        prices_as_of: Callable[[], date],
        now_ms: Callable[[], int],
        lanes: Sequence[str] = ("metered",),
    ) -> None:
        self.registry = CollectorRegistry()
        self._ledger = ledger
        self._lane_status = lane_status
        self._project_caps = project_caps
        self._prices_as_of = prices_as_of
        self._now = now_ms
        self._lanes = tuple(lanes)
        self._projects = tuple(projects)
        reg = self.registry

        self.requests_total = Counter(
            "gateway_requests_total", "requests by outcome", ["lane", "project", "outcome"], registry=reg
        )
        self.request_duration = Histogram(
            "gateway_request_duration_seconds",
            "end-to-end request latency",
            ["lane"],
            buckets=(0.1, 0.5, 1, 2, 5, 15, 60, 300, 600),
            registry=reg,
        )
        self.billed_usd_total = Counter(
            "gateway_billed_usd_total", "billed USD settled", ["lane", "project"], registry=reg
        )
        self.list_usd_total = Counter(
            "gateway_list_usd_total", "list-equivalent USD settled", ["lane", "project"], registry=reg
        )
        self.tokens_total = Counter("gateway_tokens_total", "settled tokens", ["lane", "model", "kind"], registry=reg)
        self.swept_usd_total = Counter(
            "gateway_swept_usd_total",
            "reservations settled blind at boot (billed USD)",
            ["project"],
            registry=reg,
        )
        self.fallback_total = Counter(
            "gateway_fallback_total", "lane fallbacks with consent", ["from", "to", "project"], registry=reg
        )
        self.settle_over_reserve_total = Counter(
            "gateway_settle_over_reserve_total", "settlements above their reservation", registry=reg
        )
        self.refusals_total = Counter("gateway_refusals_total", "402 refusals by scope", ["scope"], registry=reg)
        self.upstream_errors_total = Counter(
            "gateway_upstream_errors_total", "provider failures by kind", ["lane", "kind"], registry=reg
        )

        for lane in self._lanes:
            self.request_duration.labels(lane)
            for kind in UPSTREAM_KINDS:
                self.upstream_errors_total.labels(lane, kind)
            for project in self._projects:
                self.billed_usd_total.labels(lane, project)
                self.list_usd_total.labels(lane, project)
                for outcome in OUTCOMES:
                    self.requests_total.labels(lane, project, outcome)
        for project in self._projects:
            self.swept_usd_total.labels(project)
        for scope in REFUSAL_SCOPES:
            self.refusals_total.labels(scope)
        reg.register(_Sampled(self._gauges))

    def _gauges(self) -> list[Metric]:
        now = self._now()
        out: list[Metric] = []

        db_bytes = GaugeMetricFamily("gateway_db_bytes", "size of gateway.db")
        db_bytes.add_metric([], float(self._ledger.db_bytes()))
        out.append(db_bytes)

        count, oldest = self._ledger.in_flight()
        in_flight = GaugeMetricFamily("gateway_reservations_in_flight", "reserved rows")
        in_flight.add_metric([], float(count))
        out.append(in_flight)
        oldest_age = GaugeMetricFamily("gateway_reservation_oldest_age_seconds", "age of the oldest reservation")
        oldest_age.add_metric([], 0.0 if oldest is None else max(0.0, (now - oldest) / 1000))
        out.append(oldest_age)

        period, _ = period_day(now)
        spent = GaugeMetricFamily(
            "gateway_project_period_spent_usd", "billed USD committed this period", labels=["project"]
        )
        cap = GaugeMetricFamily("gateway_project_period_cap_usd", "project period cap in USD", labels=["project"])
        for project, cap_micro in self._project_caps().items():
            totals = self._ledger.project_totals(project, period)
            spent.add_metric([project], micro_to_usd_float(totals.committed("billed")))
            cap.add_metric([project], micro_to_usd_float(cap_micro))
        out.extend([spent, cap])

        status = self._lane_status()
        lane_up = GaugeMetricFamily("gateway_lane_up", "1 when the lane accepts requests", labels=["lane"])
        lane_cooling = GaugeMetricFamily("gateway_lane_cooling", "1 while the lane is cooling", labels=["lane"])
        lane_auth = GaugeMetricFamily("gateway_lane_auth_ok", "0 when the lane credential failed", labels=["lane"])
        lane_up.add_metric([status.lane], 1.0 if status.up else 0.0)
        cooling = status.cooling_until is not None and now < status.cooling_until
        lane_cooling.add_metric([status.lane], 1.0 if cooling else 0.0)
        lane_auth.add_metric([status.lane], 1.0 if status.auth_ok else 0.0)
        out.extend([lane_up, lane_cooling, lane_auth])

        brake = GaugeMetricFamily("gateway_brake_tripped", "1 while the daily brake is tripped", labels=["lane"])
        for lane in self._lanes:
            brake.add_metric([lane], 1.0 if self._ledger.brake_state(lane).tripped else 0.0)
        out.append(brake)

        age = GaugeMetricFamily("gateway_price_table_age_seconds", "seconds since prices_as_of")
        as_of = datetime.combine(self._prices_as_of(), datetime.min.time(), tzinfo=UTC)
        age.add_metric([], max(0.0, now / 1000 - as_of.timestamp()))
        out.append(age)
        return out
