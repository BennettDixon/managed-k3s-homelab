"""Boot sequence (spec §1, §8): config → DB → registry → quick_check → sweep → recompute → listen.

Nothing here may crash-loop the pod for a condition the failure matrix wants
answered by readiness: a malformed or full database yields an alive-but-
NotReady process with the reason in its log and its /readyz body.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import Callable

import uvicorn

from gateway import __version__
from gateway.config import Config, load_config
from gateway.db import open_db
from gateway.http import AppState, build_app
from gateway.jsonlog import Log, stdout_log
from gateway.lanes import LaneStatus, MeteredLane
from gateway.ledger import Ledger, LedgerUnavailable
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
    boot_errors: list[str] = []
    db_error: str | None = None
    try:
        conn = open_db(config.db_path)
    except sqlite3.Error as err:
        # Alive but NotReady: the ledger endpoints and every money path stay
        # blocked, the reason is in the log, and a human decides.
        db_error = f"database open/migrate failed: {err}"
        log("db_open_failed", error=str(err), path=config.db_path)
        boot_errors.append(db_error)
        conn = open_db(":memory:")
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
        # Advisory state: written off the event loop, never awaited.
        def write() -> None:
            ledger.set_lane_state(
                status.lane, last_heartbeat=status.last_ok, cooling_until=status.cooling_until, auth_ok=status.auth_ok
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            write()
            return
        loop.run_in_executor(None, write)

    lane = MeteredLane(now_ms=now_ms, log=log, persist=persist_lane)
    metrics = Metrics(
        projects=list(registry.projects),
        ledger=ledger,
        lane_status=lane.status,
        project_caps=lambda: {p.id: p.cap_micro for p in registry.projects.values()},
        prices_as_of=lambda: registry.prices_as_of,
        now_ms=now_ms,
    )

    swept_count = 0
    if db_error is None:
        # Integrity first: a corrupt file is never written to, not even by the sweep.
        if not ledger.quick_check():
            boot_errors.append("PRAGMA quick_check failed")
            log("quick_check_failed", path=config.db_path)
        else:
            # Sweep BEFORE listen: any reserved row is an orphan of a previous process.
            try:
                swept = ledger.sweep(now_ms())
            except (sqlite3.Error, LedgerUnavailable) as err:
                boot_errors.append(f"boot sweep failed: {err}")
                log("sweep_failed", error=str(err))
            else:
                swept_count = swept.count
                for project, micro in swept.swept_billed_micro_by_project.items():
                    metrics.swept_usd_total.labels(project).inc(micro / 1_000_000)
                if swept.count:
                    log(
                        "sweep_done",
                        count=swept.count,
                        swept_usd_by_project={
                            p: micro_to_usd_str(m) for p, m in swept.swept_billed_micro_by_project.items()
                        },
                    )
            try:
                mismatches = ledger.recompute_totals()
            except sqlite3.Error as err:
                boot_errors.append(f"totals recompute failed: {err}")
            else:
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
                    try:
                        ledger.meta_set("prices_hash", registry.prices_hash)
                    except (sqlite3.Error, LedgerUnavailable) as err:
                        # The readiness write-ping reports the same disk; nothing to add.
                        log("prices_hash_write_failed", error=str(err))

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
        db_error=db_error,
    )
    log(
        "boot",
        version=__version__,
        projects=sorted(registry.projects),
        callers=sorted(registry.callers),
        models=sorted(registry.models),
        swept=swept_count,
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
        # Below terminationGracePeriodSeconds (30 s, slice 2): in-flight calls get
        # cancelled and settle at the reservation before the pod is killed.
        timeout_graceful_shutdown=25,
        lifespan="on",
    )
