"""Boot sequence (spec §1, §8): config → DB → registry → sweep → quick_check → recompute → listen."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import uvicorn

from gateway import __version__
from gateway.config import Config, load_config
from gateway.db import open_db
from gateway.http import AppState, build_app
from gateway.jsonlog import Log, stdout_log
from gateway.lanes import LaneStatus, MeteredLane
from gateway.ledger import Ledger
from gateway.metrics import Metrics
from gateway.money import micro_to_usd_str
from gateway.registry import Registry, RegistryError, load_registry
from gateway.service import GatewayService
from gateway.upstream import AnthropicMeteredClient, MeteredClient


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def boot(
    config: Config,
    *,
    upstream: MeteredClient,
    log: Log = stdout_log,
    now_ms: Callable[[], int] = wall_clock_ms,
) -> AppState:
    conn = open_db(config.db_path)
    ledger = Ledger(conn, now_ms=now_ms, log=log)

    # Registry parse failure fails READINESS, not the process: a bad registry
    # PR yields an alive-but-NotReady pod, never CrashLoopBackOff.
    registry_error: str | None = None
    try:
        registry = load_registry(config.registry_path, max_request_cap_micro=config.max_request_cap_micro)
    except (RegistryError, OSError) as err:
        registry = Registry.none()
        registry_error = str(err)
        log("registry_parse_failed", error=registry_error)

    def persist_lane(status: LaneStatus) -> None:
        ledger.set_lane_state(
            status.lane,
            last_heartbeat=status.last_ok,
            cooling_until=status.cooling_until,
            auth_ok=status.auth_ok,
        )

    lane = MeteredLane(now_ms=now_ms, log=log, persist=persist_lane)
    metrics = Metrics(
        projects=list(registry.projects),
        ledger=ledger,
        lane_status=lane.status,
        project_caps=lambda: {p.id: p.cap_micro for p in registry.projects.values()},
        prices_as_of=lambda: registry.prices_as_of,
        now_ms=now_ms,
    )

    boot_errors: list[str] = []
    # Sweep BEFORE listen: any reserved row is an orphan of a previous process.
    boot_ts = now_ms()
    swept = ledger.sweep(boot_ts)
    for project, micro in swept.swept_billed_micro_by_project.items():
        metrics.swept_usd_total.labels(project).inc(micro / 1_000_000)
    if swept.count:
        log(
            "sweep_done",
            count=swept.count,
            swept_usd_by_project={p: micro_to_usd_str(m) for p, m in swept.swept_billed_micro_by_project.items()},
        )
    if not ledger.quick_check():
        boot_errors.append("PRAGMA quick_check failed")
    mismatches = ledger.recompute_totals()
    if mismatches:
        boot_errors.append(f"scope_totals mismatch ({len(mismatches)} rows)")
        for line in mismatches[:20]:
            log("scope_totals_mismatch", detail=line)
    if not registry.empty:
        previous = ledger.meta_get("prices_hash")
        if previous != registry.prices_hash:
            log(
                "prices_hash",
                previous=previous,
                current=registry.prices_hash,
                prices_as_of=str(registry.prices_as_of),
            )
            ledger.meta_set("prices_hash", registry.prices_hash)

    service = GatewayService(
        config=config,
        registry=registry,
        ledger=ledger,
        upstream=upstream,
        lane=lane,
        metrics=metrics,
        log=log,
        now_ms=now_ms,
    )
    state = AppState(
        config=config,
        registry=registry,
        registry_error=registry_error,
        ledger=ledger,
        service=service,
        metrics=metrics,
        lane=lane,
        upstream=upstream,
        log=log,
        now_ms=now_ms,
        boot_errors=boot_errors,
    )
    log(
        "boot",
        version=__version__,
        projects=sorted(registry.projects),
        callers=sorted(registry.callers),
        models=sorted(registry.models),
        swept=swept.count,
        degraded=state.ready_reason(),
    )
    return state


def main() -> None:
    config = load_config(os.environ)
    state = boot(config, upstream=AnthropicMeteredClient(config.anthropic_api_key))
    app = build_app(state)
    state.log("listening", port=config.port)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 — tailnet-only pod; the Service is the perimeter
        port=config.port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=25,
        lifespan="on",
    )
