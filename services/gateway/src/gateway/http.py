"""HTTP surface (spec §3): the OpenAI routes, the ledger reads, the probes.

The deny-by-default capability table is ONE ASGI middleware keyed by path
prefix and dispatched before any handler: a handler cannot be reached
ungated, and a prefix the table does not know answers 404 without
authentication. ``/lane/*`` and ``/ledger/*`` sit outside ``/v1/*`` so the
OpenAI surface stays exactly OpenAI.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import cast

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import __version__
from gateway.auth import resolve_caller
from gateway.config import Config
from gateway.errors import (
    GatewayError,
    forbidden,
    internal,
    ledger_unavailable,
    not_found,
    schema,
    unauthorized,
    unsupported,
)
from gateway.jsonlog import Log
from gateway.lanes import MeteredLane
from gateway.ledger import Ledger, LedgerUnavailable, period_day
from gateway.metrics import Metrics
from gateway.money import micro_to_usd_float
from gateway.registry import Caller, Registry
from gateway.service import GatewayService
from gateway.upstream import MeteredClient, UpstreamFailure

SPENDING: tuple[str, ...] = ("operator", "executor", "worker")
LEDGER_READ: tuple[str, ...] = ("operator",)
JOB_READ: tuple[str, ...] = ("operator", "executor")
LANE_AGENT: tuple[str, ...] = ("lane-agent",)
OPEN_ROUTES: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})
REQUESTS_PAGE_LIMIT = 500
# Validation-class rejections counted as outcome="rejected" (spec §9) once the project is known.
REJECTED_STATUSES: frozenset[int] = frozenset({400, 403, 409, 413})


def classes_for(path: str) -> tuple[str, ...] | None:
    """The capability table (spec §2). None = open route; () = nobody (unknown prefix ⇒ 404)."""
    if path in OPEN_ROUTES:
        return None
    if path.startswith("/v1/"):
        return SPENDING
    if path.startswith("/ledger/jobs/"):
        return JOB_READ
    if path.startswith("/ledger/"):
        return LEDGER_READ
    if path.startswith("/lane/"):
        return LANE_AGENT
    return ()


@dataclass
class AppState:
    config: Config
    registry: Registry
    registry_error: str | None
    ledger: Ledger
    service: GatewayService
    metrics: Metrics
    lane: MeteredLane
    upstream: MeteredClient
    log: Log
    now_ms: Callable[[], int]
    boot_errors: list[str] = field(default_factory=list)
    db_error: str | None = None

    def ready_reason(self) -> str | None:
        """Why /readyz would say no (None = ready). Money paths are blocked while set.

        Cheap and synchronous by construction: ``clock_ok`` reads a cached
        timestamp, never SQLite, so this can run on the event loop.
        """
        if self.registry_error is not None:
            return f"registry parse failed: {self.registry_error}"
        if not self.config.caller_tokens:
            return "caller tokens missing"
        if not self.config.anthropic_api_key:
            return "metered key missing"
        if self.boot_errors:
            return "; ".join(self.boot_errors)
        if not self.ledger.clock_ok(self.now_ms()):
            return "clock guard: now is behind the last committed transaction"
        return None


def error_response(err: GatewayError) -> JSONResponse:
    return JSONResponse(err.envelope(), status_code=err.status, headers=err.response_headers())


def money_row(row: Mapping[str, object]) -> dict[str, object]:
    """A ledger row for /ledger/*: the metadata columns, micro-USD rendered as USD alongside."""
    out = dict(row)
    for column in ("cap_presented_micro", "reserved_micro", "settled_micro", "list_reserved_micro", "list_micro"):
        value = out.get(column)
        out[column.replace("_micro", "_usd")] = None if value is None else micro_to_usd_float(int(cast(int, value)))
    return out


def caller_of(request: Request) -> Caller:
    return cast(Caller, request.state.caller)


class ClassGate:
    """Authenticate and class-gate every request by path prefix, before routing."""

    def __init__(self, app: ASGIApp, state: AppState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope["path"])
        classes = classes_for(path)
        if classes is None:
            await self.app(scope, receive, send)
            return
        try:
            if not classes:
                raise not_found("no such route")
            if self.state.registry_error is not None:
                raise ledger_unavailable("not ready: registry parse failed")
            headers = Headers(scope=scope)
            caller = resolve_caller(headers.get("authorization"), self.state.config.caller_tokens, self.state.registry)
            if caller is None:
                raise unauthorized()
            if caller.class_ not in classes:
                raise forbidden(f"class {caller.class_} may not call {path}")
        except GatewayError as err:
            await error_response(err)(scope, receive, send)
            return
        scope.setdefault("state", {})["caller"] = caller
        await self.app(scope, receive, send)


def build_app(state: AppState) -> Starlette:
    log = state.log

    async def read_body_capped(request: Request) -> bytes:
        limit = state.config.body_limit_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise GatewayError("E_SCHEMA", 413, f"body exceeds {limit} bytes")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise GatewayError("E_SCHEMA", 413, f"body exceeds {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def read_json(request: Request) -> object:
        raw = await read_body_capped(request)
        if not raw:
            raise schema("request body is required")
        try:
            return json.loads(raw)
        except ValueError as err:
            raise schema("request body is not valid JSON") from err

    def require_ledger() -> None:
        if state.db_error is not None:
            raise ledger_unavailable(f"not ready: {state.db_error}")

    def require_ready() -> None:
        reason = state.ready_reason()
        if reason is not None:
            raise ledger_unavailable(f"not ready: {reason}")

    # ------------------------------------------------------------- probes

    async def healthz(_: Request) -> Response:
        try:
            await asyncio.to_thread(state.ledger.read_ping)
        except Exception as err:
            log("healthz_failed", error=str(err))
            return JSONResponse({"ok": False}, status_code=500)
        return JSONResponse({"ok": True, "version": __version__})

    async def readyz(_: Request) -> Response:
        reason = state.ready_reason()
        if reason is None:
            try:
                await asyncio.to_thread(state.ledger.write_ping)
            except LedgerUnavailable as err:
                reason = f"write ping failed: {err}"
        if reason is not None:
            return JSONResponse({"ok": False, "reason": reason}, status_code=503)
        return JSONResponse({"ok": True, "version": __version__})

    async def metrics(_: Request) -> Response:
        try:
            payload = await asyncio.to_thread(generate_latest, state.metrics.registry)
        except Exception as err:
            log("metrics_scrape_failed", error=str(err))
            return Response("metrics collection failed", status_code=500)
        return Response(payload, media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------- /v1

    def count_rejected(request: Request, err: GatewayError) -> None:
        if err.status not in REJECTED_STATUSES:
            return
        project = request.headers.get("x-gateway-project") or request.headers.get("openai-project") or ""
        if project in state.registry.projects:
            state.metrics.requests_total.labels("metered", project, "rejected").inc()

    def retrieve(task: asyncio.Task[object]) -> None:
        # A detached money task whose client left: retrieve the exception so it is logged, not warned about.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not isinstance(exc, GatewayError):
            log("stream_task_failed", error=repr(exc))

    async def chat_completions(request: Request) -> Response:
        caller = caller_of(request)
        require_ready()
        body = await read_json(request)
        try:
            prepared = await state.service.prepare(caller, request.headers, body)
        except GatewayError as err:
            count_rejected(request, err)
            raise
        if not prepared.stream:
            done = await state.service.execute(prepared)
            return JSONResponse(done.body, headers=done.headers)

        # Buffered stream (spec §3): headers go out first so no client dies at
        # 300 s waiting; the money fields ride the final chunk's gateway object.
        # The money path runs as its own task: a client disconnect cancels this
        # response's generator, never the provider call or the settle — the
        # row ends settled from real usage (≤ the reservation) either way.
        task = asyncio.create_task(state.service.execute(prepared))
        task.add_done_callback(retrieve)

        async def events() -> AsyncIterator[str]:
            try:
                done = await asyncio.shield(task)
            except GatewayError as err:
                yield f"data: {json.dumps(err.envelope(), separators=(',', ':'))}\n\n"
            except Exception as err:
                log("stream_unhandled", request_id=prepared.reservation.id, error=repr(err))
                yield f"data: {json.dumps(internal().envelope(), separators=(',', ':'))}\n\n"
            else:
                for payload in done.sse:
                    yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"

        headers = {
            "X-Gateway-Request-Id": prepared.reservation.id,
            "X-Gateway-Lane-Used": prepared.lane_used,
            "x-gateway-stream": "buffered",
            "cache-control": "no-cache",
        }
        if prepared.ignored:
            headers["X-Gateway-Ignored"] = ",".join(prepared.ignored)
        if prepared.fallback_from is not None:
            headers["X-Gateway-Fallback-From"] = prepared.fallback_from
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    async def models(request: Request) -> Response:
        caller = caller_of(request)
        registry = state.registry
        header = request.headers.get("x-gateway-project") or request.headers.get("openai-project")
        if header is not None:
            project_ids = [state.service.project_for(caller, request.headers).id]
        else:
            project_ids = [p for p in caller.projects if p in registry.projects]
        seen: dict[str, dict[str, object]] = {}
        for project_id in project_ids:
            project = registry.projects[project_id]
            for model_id in project.models:
                model = registry.models[model_id]
                entry = seen.setdefault(
                    model_id,
                    {
                        "id": model_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "anthropic",
                        "aliases": sorted(a for a, target in registry.aliases.items() if target == model_id),
                        "lanes": sorted(model.lanes),
                        "max_tokens": model.max_tokens,
                        "default_max_tokens": model.default_max_tokens,
                        "effort": model.effort,
                        "projects": [],
                    },
                )
                cast(list[str], entry["projects"]).append(project_id)
        return JSONResponse({"object": "list", "data": list(seen.values())})

    async def embeddings(_: Request) -> Response:
        raise unsupported("/v1/embeddings is declared but not offered in v1 (spec §11)", status=501)

    # ------------------------------------------------------------- /ledger

    async def ledger_project(request: Request) -> Response:
        caller = caller_of(request)
        require_ledger()
        project_id = request.path_params["project_id"]
        project = state.registry.projects.get(project_id)
        if project is None or project_id not in caller.projects:
            raise forbidden("project not granted to this caller")
        period, _ = period_day(state.now_ms())
        totals = await asyncio.to_thread(state.ledger.project_totals, project_id, period)
        return JSONResponse(
            {
                "project": project_id,
                "period": period,
                "cap_usd": micro_to_usd_float(project.cap_micro),
                "held_billed_usd": micro_to_usd_float(totals.held_billed),
                "settled_billed_usd": micro_to_usd_float(totals.settled_billed),
                "held_list_usd": micro_to_usd_float(totals.held_list),
                "settled_list_usd": micro_to_usd_float(totals.settled_list),
                "remaining_usd": micro_to_usd_float(max(0, project.cap_micro - totals.committed("billed"))),
                "lanes": sorted(project.lanes),
                "default_lane": project.default_lane,
                "fallback_allowed": project.fallback_allowed,
                "max_request_cap_usd": micro_to_usd_float(project.max_request_cap_micro),
                "models": list(project.models),
            }
        )

    async def ledger_job(request: Request) -> Response:
        caller = caller_of(request)
        require_ledger()
        job_id = request.path_params["job_id"]
        # Scope key is (caller_id, job_id): an executor reads only its own jobs;
        # an operator may name another caller whose projects it is granted.
        target = caller.id
        requested = request.query_params.get("caller")
        if requested is not None and requested != caller.id:
            other = state.registry.callers.get(requested)
            if caller.class_ != "operator" or other is None or not set(other.projects) <= set(caller.projects):
                raise forbidden("caller not visible to this reader")
            target = requested
        cap = await asyncio.to_thread(state.ledger.job_cap, target, job_id)
        if cap is None:
            raise not_found("no ledger rows for this job")
        totals = await asyncio.to_thread(state.ledger.job_totals, target, job_id)
        count = await asyncio.to_thread(state.ledger.job_request_count, target, job_id)
        return JSONResponse(
            {
                "caller_id": target,
                "job_id": job_id,
                "cap_usd": micro_to_usd_float(cap),
                "spent_usd": micro_to_usd_float(totals.settled_list),
                "held_list_usd": micro_to_usd_float(totals.held_list),
                "settled_billed_usd": micro_to_usd_float(totals.settled_billed),
                "held_billed_usd": micro_to_usd_float(totals.held_billed),
                "remaining_usd": micro_to_usd_float(max(0, cap - totals.committed("list"))),
                "requests": count,
            }
        )

    async def ledger_requests(request: Request) -> Response:
        caller = caller_of(request)
        require_ledger()
        project_id = request.query_params.get("project")
        if not project_id:
            raise schema("query parameter project is required", param="project")
        if project_id not in state.registry.projects or project_id not in caller.projects:
            raise forbidden("project not granted to this caller")
        since_raw = request.query_params.get("since", "0")
        try:
            since = int(since_raw)
        except ValueError as err:
            raise schema("since must be unix milliseconds", param="since") from err
        rows = await asyncio.to_thread(state.ledger.requests_for_project, project_id, since, REQUESTS_PAGE_LIMIT)
        return JSONResponse({"project": project_id, "since": since, "requests": [money_row(r) for r in rows]})

    async def ledger_lanes(_: Request) -> Response:
        require_ledger()
        status = state.lane.status()
        brake = await asyncio.to_thread(state.ledger.brake_state, "metered")
        return JSONResponse(
            {
                "lanes": [
                    {
                        "lane": status.lane,
                        "up": status.up,
                        "auth_ok": status.auth_ok,
                        "cooling_until": status.cooling_until,
                        "down_until": status.down_until,
                        "last_ok": status.last_ok,
                    },
                    {"lane": "subscription", "up": False, "deferred": True},
                ],
                "brakes": [
                    {
                        "lane": brake.lane,
                        "tripped": brake.tripped,
                        "tripped_at": brake.tripped_at,
                        "reason": brake.reason,
                        "reset_at": brake.reset_at,
                        "reset_by": brake.reset_by,
                        "ceiling_usd": micro_to_usd_float(state.config.metered_daily_ceiling_micro),
                    }
                ],
            }
        )

    async def brake_reset(request: Request) -> Response:
        caller = caller_of(request)
        require_ready()  # a money-adjacent write: same gate as a reservation
        body = await read_json(request)
        if not isinstance(body, dict):
            raise schema("body must be an object")
        lane = body.get("lane")
        reason = body.get("reason")
        if lane not in ("metered", "subscription"):
            raise schema("lane must be metered or subscription", param="lane")
        if not isinstance(reason, str) or not reason.strip():
            raise schema("reason is required", param="reason")
        brake = await asyncio.to_thread(state.ledger.reset_brake, str(lane), reason=reason.strip(), reset_by=caller.id)
        return JSONResponse(
            {"lane": brake.lane, "tripped": brake.tripped, "reset_at": brake.reset_at, "reset_by": brake.reset_by}
        )

    # ------------------------------------------------------------- /lane (deferred)

    async def lane_stub(_: Request) -> Response:
        raise unsupported("the subscription lane is deferred in v1 (spec §6.2)", status=501)

    # ------------------------------------------------------------- errors

    async def on_gateway_error(_: Request, exc: Exception) -> Response:
        return error_response(cast(GatewayError, exc))

    async def on_http_exception(_: Request, exc: Exception) -> Response:
        http_exc = cast(HTTPException, exc)
        if http_exc.status_code == 404:
            return error_response(not_found("no such route"))
        if http_exc.status_code == 405:
            return error_response(
                GatewayError("E_SCHEMA", 405, "method not allowed", headers=dict(http_exc.headers or {}))
            )
        return error_response(GatewayError("E_SCHEMA", http_exc.status_code, str(http_exc.detail)))

    async def on_client_disconnect(_: Request, __: Exception) -> Response:
        # An aborted upload: nobody is listening for the answer; keep it out of the unhandled log.
        return Response(status_code=499)

    async def on_unhandled(request: Request, exc: Exception) -> Response:
        log("unhandled", path=request.url.path, error=repr(exc))
        return error_response(internal())

    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        probe = asyncio.create_task(probe_loop(state)) if state.config.lane_probe_interval_ms > 0 else None
        try:
            yield
        finally:
            if probe is not None:
                probe.cancel()
                with suppress(asyncio.CancelledError):
                    await probe

    routes = [
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        Route("/metrics", metrics),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", models),
        Route("/v1/embeddings", embeddings, methods=["POST"]),
        Route("/ledger/projects/{project_id}", ledger_project),
        Route("/ledger/jobs/{job_id}", ledger_job),
        Route("/ledger/requests", ledger_requests),
        Route("/ledger/lanes", ledger_lanes),
        Route("/ledger/brake-reset", brake_reset, methods=["POST"]),
        Route("/lane/claim", lane_stub, methods=["POST"]),
        Route("/lane/heartbeat", lane_stub, methods=["POST"]),
        Route("/lane/complete", lane_stub, methods=["POST"]),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(ClassGate, state=state)],
        exception_handlers={
            GatewayError: on_gateway_error,
            HTTPException: on_http_exception,
            ClientDisconnect: on_client_disconnect,
            Exception: on_unhandled,
        },
        lifespan=asynccontextmanager(lifespan),
    )


async def probe_loop(state: AppState) -> None:
    """Idle probe (spec §6.1): every 5 min while up, every 60 s while down."""
    while True:
        status = state.lane.status()
        interval = state.config.lane_probe_interval_ms if status.up else state.config.lane_probe_down_interval_ms
        await asyncio.sleep(interval / 1000)
        now = state.now_ms()
        try:
            await state.upstream.probe()
            state.lane.probe_ok(now)
        except UpstreamFailure as failure:
            state.metrics.upstream_errors_total.labels(state.lane.lane, failure.kind).inc()
            state.log("lane_probe_failed", lane=state.lane.lane, kind=failure.kind, status=failure.status)
            state.lane.probe_failed(now, failure.kind)
        except Exception as err:
            state.log("lane_probe_error", error=repr(err))
