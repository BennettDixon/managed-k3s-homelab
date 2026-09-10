"""Cancellation and client-disconnect paths (spec §1, §10): the money write always lands.

The ASGI app is driven directly with a uvicorn-shaped scope (spec_version
2.3) so Starlette's disconnect listener runs exactly as it does under
uvicorn; httpx's ASGITransport never disconnects mid-response.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest

from gateway.config import load_config
from gateway.fake_upstream import FakeFailure, FakeHang, FakeMeteredClient, FakeReply, Outcome
from gateway.http import AppState, build_app
from gateway.main import boot
from tests.conftest import TOKENS, FakeClock, LogCapture, base_env, make_harness

BODY_STREAM = {"model": "haiku", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5, "stream": True}
BODY_PLAIN = {"model": "haiku", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}


async def drive(
    tmp_path: Path, script: list[Outcome], *, body: dict[str, object], disconnect_after_s: float
) -> tuple[AppState, FakeMeteredClient, list[str]]:
    log = LogCapture()
    upstream = FakeMeteredClient(script=script)
    state = boot(load_config(base_env(tmp_path)), upstream=upstream, log=log, now_ms=FakeClock().now_ms)
    app = build_app(state)
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway"),
            (b"authorization", f"Bearer {TOKENS['operator']}".encode()),
            (b"x-gateway-project", b"homelab-ops"),
            (b"x-gateway-budget-cap-usd", b"1.00"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8080),
        "state": {},
    }
    queue = [{"type": "http.request", "body": raw, "more_body": False}]
    sent: list[str] = []
    disconnected = False

    async def receive() -> MutableMapping[str, Any]:
        nonlocal disconnected
        if queue:
            return queue.pop(0)
        if not disconnected:
            await asyncio.sleep(disconnect_after_s)
            disconnected = True
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if not disconnected:
            sent.append(str(message["type"]))

    await asyncio.wait_for(app(scope, receive, send), timeout=5.0)
    return state, upstream, sent


async def wait_for_state(state: AppState, wanted: str, timeout_s: float = 3.0) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        rows = state.ledger.requests_for_project("homelab-ops", 0)
        if rows and rows[0]["state"] == wanted:
            return rows[0]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"row never reached {wanted}: {rows}")
        await asyncio.sleep(0.02)


async def test_stream_disconnect_mid_call_settles_from_usage(tmp_path: Path) -> None:
    state, upstream, _ = await drive(tmp_path, [FakeReply(delay_s=0.3)], body=BODY_STREAM, disconnect_after_s=0.05)
    row = await wait_for_state(state, "settled")
    settled, reserved = row["settled_micro"], row["reserved_micro"]
    assert isinstance(settled, int) and isinstance(reserved, int) and settled < reserved
    assert row["upstream_started"] == 1
    assert state.ledger.in_flight() == (0, None) and upstream.generations == 1
    assert state.ledger.recompute_totals() == []


async def test_stream_client_gone_before_the_response_starts(tmp_path: Path) -> None:
    state, _, _ = await drive(tmp_path, [FakeReply()], body=BODY_STREAM, disconnect_after_s=0.0)
    row = await wait_for_state(state, "settled")
    assert row["error_code"] is None and state.ledger.in_flight() == (0, None)


async def test_stream_disconnect_during_an_upstream_failure_still_releases(tmp_path: Path) -> None:
    state, _, _ = await drive(tmp_path, [FakeFailure("server", status=500)], body=BODY_STREAM, disconnect_after_s=0.0)
    row = await wait_for_state(state, "released")
    assert row["settled_micro"] == 0


async def test_non_stream_disconnect_still_settles(tmp_path: Path) -> None:
    state, _, _ = await drive(tmp_path, [FakeReply(delay_s=0.3)], body=BODY_PLAIN, disconnect_after_s=0.05)
    await wait_for_state(state, "settled")
    assert state.ledger.in_flight() == (0, None)


async def test_task_cancellation_aborts_at_the_reservation_synchronously(tmp_path: Path) -> None:
    h = await make_harness(tmp_path, upstream=FakeMeteredClient(script=[FakeHang(started=True)]))
    try:
        caller = h.state.registry.callers["operator"]
        headers = {k.lower(): v for k, v in h.headers().items()}
        prepared = await h.state.service.prepare(caller, headers, BODY_PLAIN)
        task = asyncio.create_task(h.state.service.execute(prepared))
        await asyncio.sleep(0.05)  # reach the hang (message_start already emitted)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = h.ledger.request_row(prepared.reservation.id)
        assert row is not None
        assert row["state"] == "aborted" and row["settled_micro"] == row["reserved_micro"]
        assert row["error_code"] == "E_ABORTED" and row["http_status"] == 499 and row["upstream_started"] == 1
        settle = [line for line in h.log.events("settle") if line.get("error_code") == "E_ABORTED"]
        assert settle and settle[0]["state"] == "aborted"
        text = (await h.client.get("/metrics")).text
        assert 'gateway_requests_total{lane="metered",outcome="upstream_error",project="homelab-ops"} 1.0' in text
        assert h.ledger.recompute_totals() == []
    finally:
        await h.client.aclose()


async def test_cancellation_during_the_retry_wait_aborts(tmp_path: Path) -> None:
    upstream = FakeMeteredClient(script=[FakeFailure("rate_limited", status=429, retry_after_s=5.0), FakeReply()])
    h = await make_harness(tmp_path, upstream=upstream)
    try:
        caller = h.state.registry.callers["operator"]
        headers = {k.lower(): v for k, v in h.headers().items()}
        prepared = await h.state.service.prepare(caller, headers, BODY_PLAIN)
        task = asyncio.create_task(h.state.service.execute(prepared))
        await asyncio.sleep(0.05)  # inside the retry sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = h.ledger.request_row(prepared.reservation.id)
        assert row is not None and row["state"] == "aborted" and upstream.attempts == 1
        assert h.log.events("upstream_retry")
    finally:
        await h.client.aclose()
