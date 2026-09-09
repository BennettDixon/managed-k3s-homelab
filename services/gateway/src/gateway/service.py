"""One chat completion, end to end (spec §3, §5, §6.1, §6.3).

``prepare`` runs everything up to and including the reservation — headers,
the OpenAI subset, routing, the worst case, the compare-and-add — and raises
a GatewayError with no row written on any refusal. ``execute`` makes the one
provider call and settles or releases. The upstream client is used only from
here, after a row exists: no code path reaches a provider without a row.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gateway.config import Config
from gateway.errors import GatewayError, forbidden, ledger_unavailable, schema
from gateway.ids import JOB_ID_RE
from gateway.jsonlog import Log
from gateway.lanes import MeteredLane
from gateway.ledger import (
    BudgetRefusal,
    ClockGuardTripped,
    JobCapMismatch,
    Ledger,
    LedgerUnavailable,
    Reservation,
    ReserveInput,
)
from gateway.metrics import Metrics
from gateway.money import (
    MoneyError,
    component_micro,
    heuristic_input_tokens,
    micro_to_usd_float,
    micro_to_usd_str,
    reserve_input_tokens,
    usage_cost_micro,
    usd_to_micro,
)
from gateway.openai_compat import completion_body, parse_chat_request, stream_payloads, translate
from gateway.registry import Caller, Model, Project, Registry
from gateway.upstream import MeteredClient, UpstreamFailure, UpstreamRequest, UpstreamResult

RETRY_AFTER_MAX_S = 10.0
# Grace beyond the provider timeout so the SDK's own timeout fires first (tests set it to 0).
TIMEOUT_MARGIN_S = 5
COUNT_TOKENS_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 600


@dataclass(frozen=True)
class Prepared:
    reservation: Reservation
    request: UpstreamRequest
    model: Model
    project: Project
    caller: Caller
    job_id: str | None
    lane_used: str
    fallback_from: str | None
    ignored: tuple[str, ...]
    timeout_s: int
    stream: bool
    prepared_at: int


@dataclass(frozen=True)
class Completed:
    body: dict[str, object]
    headers: dict[str, str]
    sse: tuple[str, str]


class GatewayService:
    def __init__(
        self,
        *,
        config: Config,
        registry: Registry,
        ledger: Ledger,
        upstream: MeteredClient,
        lane: MeteredLane,
        metrics: Metrics,
        log: Log,
        now_ms: Callable[[], int],
    ) -> None:
        self._config = config
        self._registry = registry
        self._ledger = ledger
        self._upstream = upstream
        self._lane = lane
        self._metrics = metrics
        self._log = log
        self._now = now_ms

    # ----------------------------------------------------------------- headers

    def project_for(self, caller: Caller, headers: Mapping[str, str]) -> Project:
        primary = headers.get("x-gateway-project")
        alias = headers.get("openai-project")
        if primary is not None and alias is not None and primary != alias:
            raise schema("X-Gateway-Project and OpenAI-Project disagree", param="X-Gateway-Project")
        name = primary if primary is not None else alias
        if not name:
            raise schema("X-Gateway-Project header is required", param="X-Gateway-Project")
        project = self._registry.projects.get(name)
        # Unknown and ungranted are the same answer: no existence oracle (spec §3).
        if project is None or name not in caller.projects:
            raise forbidden("project not granted to this caller")
        return project

    def cap_for(self, caller: Caller, project: Project, headers: Mapping[str, str]) -> int:
        raw = headers.get("x-gateway-budget-cap-usd")
        if raw is None or raw.strip() == "":
            raise GatewayError(
                "E_BUDGET_CAP_MISSING",
                400,
                "X-Gateway-Budget-Cap-USD is required (no default, ever)",
                param="X-Gateway-Budget-Cap-USD",
            )
        try:
            cap = usd_to_micro(raw)
        except MoneyError as err:
            raise GatewayError(
                "E_BUDGET_CAP_INVALID",
                400,
                f"X-Gateway-Budget-Cap-USD: {err}",
                param="X-Gateway-Budget-Cap-USD",
            ) from err
        # Tightest wins (spec §5): the env fat-finger guard, the caller's ceiling, the project's.
        ceiling = min(self._config.max_request_cap_micro, project.max_request_cap_micro)
        if caller.max_request_cap_micro is not None:
            ceiling = min(ceiling, caller.max_request_cap_micro)
        if cap < 0 or cap > ceiling:
            raise GatewayError(
                "E_BUDGET_CAP_INVALID",
                400,
                f"X-Gateway-Budget-Cap-USD must be in [0, {micro_to_usd_str(ceiling)}] for this caller",
                param="X-Gateway-Budget-Cap-USD",
            )
        return cap

    def job_id_for(self, caller: Caller, headers: Mapping[str, str]) -> str | None:
        job_id = headers.get("x-gateway-job-id")
        if job_id is None:
            if caller.class_ == "executor":
                raise forbidden("class executor requires X-Gateway-Job-Id")
            return None
        if not JOB_ID_RE.match(job_id):
            raise schema(f"X-Gateway-Job-Id must match {JOB_ID_RE.pattern}", param="X-Gateway-Job-Id")
        return job_id

    def lane_headers(self, headers: Mapping[str, str], project: Project) -> tuple[str, bool, bool]:
        """(lane_requested, pinned, fallback_wanted)."""
        raw = headers.get("x-gateway-lane")
        pinned = raw is not None
        if raw is None:
            lane = project.default_lane
        elif raw == "auto":
            # The one policy this spec refuses to make configurable (spec §6.3).
            raise schema("X-Gateway-Lane: auto is rejected at admission", param="X-Gateway-Lane")
        elif raw in ("metered", "subscription"):
            lane = raw
        else:
            raise schema("X-Gateway-Lane must be metered or subscription", param="X-Gateway-Lane")
        fallback = headers.get("x-gateway-fallback")
        if fallback is not None and fallback != "metered":
            raise schema("X-Gateway-Fallback must be metered", param="X-Gateway-Fallback")
        return lane, pinned, fallback is not None

    def timeout_for(self, headers: Mapping[str, str]) -> int:
        raw = headers.get("x-gateway-timeout-s")
        default = max(1, self._config.provider_timeout_ms // 1000)
        if raw is None:
            return min(default, MAX_TIMEOUT_S)
        try:
            value = int(raw)
        except ValueError as err:
            raise schema("X-Gateway-Timeout-S must be an integer", param="X-Gateway-Timeout-S") from err
        if not 1 <= value <= MAX_TIMEOUT_S:
            raise schema(f"X-Gateway-Timeout-S must be in [1, {MAX_TIMEOUT_S}]", param="X-Gateway-Timeout-S")
        return value

    # ----------------------------------------------------------------- routing

    @staticmethod
    def _metered_granted(caller: Caller, project: Project, model: Model) -> bool:
        return "metered" in caller.lanes and "metered" in project.lanes and "metered" in model.lanes

    def route(
        self,
        caller: Caller,
        project: Project,
        model: Model,
        lane_requested: str,
        pinned: bool,
        fallback_wanted: bool,
        now: int,
    ) -> tuple[str, str | None]:
        """Returns (lane_used, fallback_from). Routing runs once, at admission."""
        fallback_from: str | None = None
        if lane_requested == "subscription":
            if "subscription" not in caller.lanes or "subscription" not in project.lanes:
                raise forbidden("subscription lane not granted")
            # v1: the lane is deferred (spec §6.2) — never eligible. A pinned
            # request never converts to spend; an unpinned one falls back only
            # with the header AND the project's bit (spec §6.3).
            if pinned or not (fallback_wanted and project.fallback_allowed):
                raise GatewayError(
                    "E_LANE_UNAVAILABLE",
                    503,
                    "subscription lane is deferred in v1 (spec §6.2)",
                    retryable=True,
                    headers={"retry-after": "3600"},
                )
            fallback_from = "subscription"
        if not self._metered_granted(caller, project, model):
            raise forbidden("metered lane not granted for this caller, project and model")
        ok, retry_after = self._lane.check(now)
        if not ok:
            self._metrics.requests_total.labels("metered", project.id, "lane_unavailable").inc()
            raise GatewayError(
                "E_LANE_UNAVAILABLE",
                503,
                "metered lane is down",
                retryable=True,
                headers={"retry-after": str(retry_after)},
            )
        return "metered", fallback_from

    # ----------------------------------------------------------------- prepare

    async def _estimate_input_tokens(self, request: UpstreamRequest) -> int:
        try:
            counted = await asyncio.wait_for(self._upstream.count_tokens(request), timeout=COUNT_TOKENS_TIMEOUT_S)
        except (UpstreamFailure, TimeoutError, OSError) as err:
            # A count_tokens outage degrades accuracy, never availability (spec §5).
            self._metrics.upstream_errors_total.labels("metered", "count_tokens").inc()
            self._log("count_tokens_fallback", error=str(err))
            return heuristic_input_tokens(request.text_bytes())
        return reserve_input_tokens(counted)

    async def prepare(self, caller: Caller, headers: Mapping[str, str], raw_body: object) -> Prepared:
        now = self._now()
        project = self.project_for(caller, headers)
        cap_micro = self.cap_for(caller, project, headers)
        job_id = self.job_id_for(caller, headers)
        lane_requested, pinned, fallback_wanted = self.lane_headers(headers, project)
        timeout_s = self.timeout_for(headers)
        parsed = parse_chat_request(raw_body)
        translated = translate(parsed, self._registry, project)
        model = translated.model
        lane_used, fallback_from = self.route(caller, project, model, lane_requested, pinned, fallback_wanted, now)

        in_tokens = await self._estimate_input_tokens(translated.request)
        prices = model.prices
        multiplier = self._config.billed_price_multiplier_pct
        max_tokens = translated.request.max_tokens
        in_cost_list = component_micro(in_tokens, prices.input)
        in_cost_billed = component_micro(in_tokens, prices.input, multiplier)
        list_reserved = in_cost_list + component_micro(max_tokens, prices.output)
        billed_reserved = in_cost_billed + component_micro(max_tokens, prices.output, multiplier)

        inp = ReserveInput(
            caller_id=caller.id,
            caller_class=caller.class_,
            project_id=project.id,
            job_id=job_id,
            lane_requested=lane_requested,
            lane_used=lane_used,
            fallback=fallback_from is not None,
            model=model.id,
            cap_presented_micro=cap_micro,
            reserved_micro=billed_reserved,
            list_reserved_micro=list_reserved,
            max_tokens=max_tokens,
            in_tokens=in_tokens,
            in_cost_billed_micro=in_cost_billed,
            in_cost_list_micro=in_cost_list,
            output_price_micro_per_mtok=prices.output,
            billed_multiplier_pct=multiplier,
            project_cap_micro=project.cap_micro,
            caller_day_cap_micro=caller.max_day_billed_micro,
            brake_cap_micro=self._config.metered_daily_ceiling_micro,
        )
        try:
            reservation = await asyncio.to_thread(self._ledger.reserve, inp)
        except BudgetRefusal as refusal:
            outcome = "refused_cap" if refusal.scope in ("request", "job") else "refused_budget"
            self._metrics.requests_total.labels(lane_used, project.id, outcome).inc()
            self._metrics.refusals_total.labels(refusal.scope).inc()
            self._log(
                "refuse",
                caller_id=caller.id,
                project=project.id,
                job_id=job_id,
                lane=lane_used,
                model=model.id,
                scope=refusal.scope,
                basis=refusal.basis,
                would_reserve_usd=micro_to_usd_str(refusal.would_reserve_micro),
                remaining_usd=micro_to_usd_str(refusal.remaining_micro),
            )
            raise GatewayError(
                "E_BUDGET_EXCEEDED",
                402,
                f"budget exceeded on scope {refusal.scope}: reserving "
                f"{micro_to_usd_str(refusal.would_reserve_micro)} USD ({refusal.basis}) against "
                f"{micro_to_usd_str(refusal.remaining_micro)} USD remaining",
                retryable=False,
                extra={
                    "scope": refusal.scope,
                    "basis": refusal.basis,
                    "remaining_usd": micro_to_usd_float(refusal.remaining_micro),
                    "would_reserve_usd": micro_to_usd_float(refusal.would_reserve_micro),
                    "affordable_max_tokens": refusal.affordable_max_tokens,
                },
            ) from refusal
        except JobCapMismatch as mismatch:
            self._metrics.requests_total.labels(lane_used, project.id, "rejected").inc()
            raise GatewayError(
                "E_JOB_CAP_MISMATCH",
                409,
                f"job {job_id} is pinned to cap {micro_to_usd_str(mismatch.pinned_micro)} USD",
                param="X-Gateway-Budget-Cap-USD",
                extra={"pinned_cap_usd": micro_to_usd_float(mismatch.pinned_micro)},
            ) from mismatch
        except ClockGuardTripped as guard:
            self._log("clock_guard", error=str(guard))
            raise ledger_unavailable("clock guard: no reservations until the clock catches up") from guard
        except LedgerUnavailable as err:
            self._log("ledger_write_failed", error=str(err))
            raise ledger_unavailable("ledger write failed; no call was made") from err

        if fallback_from is not None:
            self._metrics.fallback_total.labels(fallback_from, lane_used, project.id).inc()
        self._log(
            "reserve",
            request_id=reservation.id,
            caller_id=caller.id,
            project=project.id,
            job_id=job_id,
            lane=lane_used,
            fallback_from=fallback_from,
            model=model.id,
            in_tokens=in_tokens,
            max_tokens=max_tokens,
            reserved_usd=micro_to_usd_str(billed_reserved),
            list_reserved_usd=micro_to_usd_str(list_reserved),
            cap_usd=micro_to_usd_str(cap_micro),
        )
        return Prepared(
            reservation=reservation,
            request=translated.request,
            model=model,
            project=project,
            caller=caller,
            job_id=job_id,
            lane_used=lane_used,
            fallback_from=fallback_from,
            ignored=parsed.ignored,
            timeout_s=timeout_s,
            stream=parsed.stream,
            prepared_at=now,
        )

    # ----------------------------------------------------------------- execute

    @staticmethod
    def _retry_once(failure: UpstreamFailure) -> bool:
        """Exactly one gateway retry: a 429 with Retry-After <= 10 s, or a connection refused before the body."""
        if failure.kind == "rate_limited":
            return (
                failure.before_generation
                and failure.retry_after_s is not None
                and failure.retry_after_s <= RETRY_AFTER_MAX_S
            )
        return failure.kind == "network" and failure.before_generation

    async def execute(self, prepared: Prepared) -> Completed:
        started_at = self._now()
        request_id = prepared.reservation.id
        started = False

        async def on_started() -> None:
            nonlocal started
            started = True
            await asyncio.to_thread(self._ledger.mark_upstream_started, request_id)

        attempt = 0
        while True:
            attempt += 1
            try:
                result = await asyncio.wait_for(
                    self._upstream.complete(
                        prepared.request, on_started=on_started, timeout_s=float(prepared.timeout_s)
                    ),
                    timeout=prepared.timeout_s + TIMEOUT_MARGIN_S,
                )
                break
            except UpstreamFailure as failure:
                if attempt == 1 and self._retry_once(failure):
                    self._log("upstream_retry", request_id=request_id, kind=failure.kind)
                    await asyncio.sleep(min(failure.retry_after_s or 0.0, RETRY_AFTER_MAX_S))
                    continue
                raise await self._fail(prepared, failure, started, started_at) from failure
            except TimeoutError as err:
                raise await self._timeout(prepared, started_at) from err
            except asyncio.CancelledError:
                await self._abort(prepared, started_at)
                raise
        return await self._settle(prepared, result, started_at)

    async def _finish(
        self, prepared: Prepared, outcome: str, *, error_code: str, http_status: int, latency_ms: int
    ) -> str:
        if outcome not in ("release", "timeout", "aborted"):
            raise ValueError(outcome)
        try:
            res = await asyncio.to_thread(
                self._ledger.finish_without_usage,
                prepared.reservation.id,
                outcome,  # type: ignore[arg-type]
                error_code=error_code,
                http_status=http_status,
                latency_ms=latency_ms,
            )
        except LedgerUnavailable as err:
            # The row stays reserved and is swept at the next boot — the safe direction.
            self._log("finish_write_failed", request_id=prepared.reservation.id, error=str(err))
            return "reserved"
        self._log(
            "release" if res.state == "released" else "settle",
            request_id=prepared.reservation.id,
            state=res.state,
            error_code=error_code,
            http_status=http_status,
            settled_usd=micro_to_usd_str(res.settled_micro),
            list_usd=micro_to_usd_str(res.list_micro),
            latency_ms=latency_ms,
        )
        return res.state

    async def _fail(self, prepared: Prepared, failure: UpstreamFailure, started: bool, started_at: int) -> GatewayError:
        now = self._now()
        latency = now - started_at
        lane = prepared.lane_used
        project = prepared.project.id
        self._metrics.upstream_errors_total.labels(lane, failure.kind).inc()
        headers: dict[str, str] = {}
        if failure.kind == "rate_limited":
            code, status, retryable = "E_UPSTREAM_RATE_LIMITED", 429, True
            headers["retry-after"] = str(max(1, int(-(-(failure.retry_after_s or 1.0) // 1))))
        elif failure.kind == "spend_limit":
            code, status, retryable = "E_LANE_UNAVAILABLE", 503, True
            resume = now + int((failure.retry_after_s or 3600.0) * 1000)
            self._lane.record_spend_limit(now, resume)
            headers["retry-after"] = str(max(1, int(failure.retry_after_s or 3600.0)))
        elif failure.kind == "auth":
            code, status, retryable = "E_UPSTREAM_AUTH", 502, False
            self._lane.record_auth_failure(now)
        elif failure.kind == "rejected":
            code, status, retryable = "E_UPSTREAM_ERROR", 502, False
        elif failure.kind == "timeout":
            code, status, retryable = "E_TIMEOUT", 504, False
        elif failure.kind in ("server", "network"):
            code, status = "E_UPSTREAM_ERROR", 502
            retryable = failure.proves_no_generation and not started
            if retryable:
                self._lane.record_transport_failure(now)
        else:
            code, status, retryable = "E_UPSTREAM_ERROR", 502, False

        release = failure.proves_no_generation and not started
        outcome = "release" if release else ("timeout" if failure.kind == "timeout" else "aborted")
        await self._finish(prepared, outcome, error_code=code, http_status=status, latency_ms=latency)
        self._metrics.requests_total.labels(
            lane, project, "timeout" if failure.kind == "timeout" else "upstream_error"
        ).inc()
        self._metrics.request_duration.labels(lane).observe(latency / 1000)
        message = {
            "E_UPSTREAM_RATE_LIMITED": "provider rate limited the request; reservation released",
            "E_LANE_UNAVAILABLE": "provider spend limit reached; lane down until the stated resume time",
            "E_UPSTREAM_AUTH": "provider rejected the gateway's credential; lane down",
            "E_TIMEOUT": "provider call timed out; settled at the reservation",
        }.get(
            code,
            "provider error before generation; reservation released"
            if release
            else "provider error; settled at the reservation",
        )
        return GatewayError(code, status, message, retryable=retryable, headers=headers)

    async def _timeout(self, prepared: Prepared, started_at: int) -> GatewayError:
        latency = self._now() - started_at
        self._metrics.upstream_errors_total.labels(prepared.lane_used, "timeout").inc()
        await self._finish(prepared, "timeout", error_code="E_TIMEOUT", http_status=504, latency_ms=latency)
        self._metrics.requests_total.labels(prepared.lane_used, prepared.project.id, "timeout").inc()
        self._metrics.request_duration.labels(prepared.lane_used).observe(latency / 1000)
        return GatewayError("E_TIMEOUT", 504, "provider call timed out; settled at the reservation", retryable=False)

    async def _abort(self, prepared: Prepared, started_at: int) -> None:
        latency = self._now() - started_at
        await self._finish(prepared, "aborted", error_code="E_ABORTED", http_status=499, latency_ms=latency)
        self._metrics.requests_total.labels(prepared.lane_used, prepared.project.id, "upstream_error").inc()

    async def _settle(self, prepared: Prepared, result: UpstreamResult, started_at: int) -> Completed:
        now = self._now()
        latency = now - started_at
        prices = prepared.model.prices
        multiplier = self._config.billed_price_multiplier_pct
        list_micro = usage_cost_micro(result.usage, prices)
        billed_micro = usage_cost_micro(result.usage, prices, multiplier)
        try:
            res = await asyncio.to_thread(
                self._ledger.settle,
                prepared.reservation.id,
                usage=result.usage,
                settled_micro=billed_micro,
                list_micro=list_micro,
                model_used=result.model_used,
                provider_request_id=result.provider_request_id,
                http_status=200,
                latency_ms=latency,
                inference_geo=result.inference_geo,
            )
        except LedgerUnavailable as err:
            # The generation happened and was billed; the row stays reserved and
            # is swept at the reservation on the next boot (over-count, never under).
            self._log("settle_write_failed", request_id=prepared.reservation.id, error=str(err))
            raise ledger_unavailable("ledger write failed after the provider call") from err
        lane = prepared.lane_used
        project = prepared.project.id
        self._lane.record_success(now)
        self._metrics.requests_total.labels(lane, project, "ok").inc()
        self._metrics.request_duration.labels(lane).observe(latency / 1000)
        if res.applied:
            self._metrics.billed_usd_total.labels(lane, project).inc(micro_to_usd_float(billed_micro))
            self._metrics.list_usd_total.labels(lane, project).inc(micro_to_usd_float(list_micro))
            usage = result.usage
            for kind, count in (
                ("input", usage.input_tokens),
                ("output", usage.output_tokens),
                ("cache_write_5m", usage.cache_write_5m_tokens),
                ("cache_write_1h", usage.cache_write_1h_tokens),
                ("cache_read", usage.cache_read_tokens),
            ):
                if count:
                    self._metrics.tokens_total.labels(lane, prepared.model.id, kind).inc(count)
            if res.over_reserve:
                self._metrics.settle_over_reserve_total.inc()
        self._log(
            "settle",
            request_id=prepared.reservation.id,
            state=res.state or "ignored",
            caller_id=prepared.caller.id,
            project=project,
            job_id=prepared.job_id,
            lane=lane,
            model=prepared.model.id,
            model_used=result.model_used,
            billed_usd=micro_to_usd_str(billed_micro),
            list_usd=micro_to_usd_str(list_micro),
            reserved_usd=micro_to_usd_str(prepared.reservation.reserved_micro),
            over_reserve=res.over_reserve,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            provider_request_id=result.provider_request_id,
            latency_ms=latency,
        )

        period_totals = await asyncio.to_thread(self._ledger.project_totals, project, prepared.reservation.period)
        remaining = max(0, prepared.project.cap_micro - period_totals.committed("billed"))
        headers: dict[str, str] = {
            "X-Gateway-Request-Id": prepared.reservation.id,
            "X-Gateway-Lane-Used": lane,
            "X-Gateway-Billed-USD": micro_to_usd_str(billed_micro),
            "X-Gateway-List-USD": micro_to_usd_str(list_micro),
            "X-Gateway-Project-Remaining-USD": micro_to_usd_str(remaining),
        }
        gateway: dict[str, object] = {
            "request_id": prepared.reservation.id,
            "lane_used": lane,
            "billed_usd": micro_to_usd_float(billed_micro),
            "list_usd": micro_to_usd_float(list_micro),
            "project_remaining_usd": micro_to_usd_float(remaining),
            "ignored": list(prepared.ignored),
        }
        if prepared.job_id is not None:
            job_totals = await asyncio.to_thread(self._ledger.job_totals, prepared.caller.id, prepared.job_id)
            headers["X-Gateway-Job-Spent-USD"] = micro_to_usd_str(job_totals.settled_list)
            gateway["job_spent_usd"] = micro_to_usd_float(job_totals.settled_list)
        if prepared.fallback_from is not None:
            headers["X-Gateway-Fallback-From"] = prepared.fallback_from
            gateway["fallback_from"] = prepared.fallback_from
        if prepared.ignored:
            headers["X-Gateway-Ignored"] = ",".join(prepared.ignored)
        created = now // 1000
        body = completion_body(
            request_id=prepared.reservation.id,
            created=created,
            model=result.model_used,
            text=result.text,
            stop_reason=result.stop_reason,
            usage=result.usage,
            gateway=gateway,
        )
        sse = stream_payloads(
            request_id=prepared.reservation.id,
            created=created,
            model=result.model_used,
            text=result.text,
            stop_reason=result.stop_reason,
            usage=result.usage,
            gateway=gateway,
        )
        return Completed(body=body, headers=headers, sse=sse)
