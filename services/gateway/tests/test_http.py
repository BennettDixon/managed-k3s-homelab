"""End to end through the ASGI app with the fake upstream: headers, routing, the ledger, the probes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from gateway import service as service_module
from gateway.db import open_db
from gateway.fake_upstream import FakeFailure, FakeHang, FakeMeteredClient, FakeReply, Outcome
from gateway.jsonlog import null_log
from gateway.ledger import Ledger, SettleResult
from gateway.money import TokenUsage, usage_cost_micro
from gateway.upstream import UpstreamFailure
from tests.conftest import REGISTRY_YAML, USD, FakeClock, Harness, make_harness, make_reserve_input


@asynccontextmanager
async def harness_ctx(tmp_path: Path, **kwargs: object) -> AsyncIterator[Harness]:
    h = await make_harness(tmp_path, **kwargs)  # type: ignore[arg-type]
    try:
        yield h
    finally:
        await h.client.aclose()


def dump(ledger: Ledger) -> str:
    conn: sqlite3.Connection = ledger._conn
    return "\n".join(conn.iterdump())


def row(h: Harness, request_id: str) -> dict[str, object]:
    found = h.ledger.request_row(request_id)
    assert found is not None
    return found


# ------------------------------------------------------------------ probes


async def test_probes_and_metrics(harness: Harness) -> None:
    assert (await harness.client.get("/healthz")).status_code == 200
    ready = await harness.client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ok"] is True
    scraped = await harness.client.get("/metrics")
    assert scraped.status_code == 200
    text = scraped.text
    assert 'gateway_requests_total{lane="metered",outcome="ok",project="homelab-ops"} 0.0' in text
    assert 'gateway_lane_up{lane="metered"} 1.0' in text
    assert 'gateway_brake_tripped{lane="metered"} 0.0' in text
    assert "gateway_db_bytes " in text and "gateway_price_table_age_seconds " in text
    assert 'gateway_project_period_cap_usd{project="gateway-smoke"} 1.0' in text


# ------------------------------------------------------------------ auth / class gate


async def test_auth_and_class_gates(harness: Harness) -> None:
    r = await harness.client.post("/v1/chat/completions", json={})
    assert r.status_code == 401 and r.json()["error"]["code"] == "E_UNAUTHORIZED"
    assert r.headers["x-should-retry"] == "false" and r.json()["error"]["type"] == "authentication_error"
    r = await harness.client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer ghost-token-0123456789"}, json={}
    )
    assert r.status_code == 401
    r = await harness.chat("worker-01")
    assert r.status_code == 403 and r.json()["error"]["code"] == "E_FORBIDDEN"
    r = await harness.client.get("/ledger/projects/gateway-smoke", headers=harness.headers("n8n-executor"))
    assert r.status_code == 403
    r = await harness.client.post("/lane/claim", headers=harness.headers("operator"), json={})
    assert r.status_code == 403
    r = await harness.client.post("/lane/claim", headers=harness.headers("worker-01"), json={})
    assert r.status_code == 501 and r.json()["error"]["code"] == "E_UNSUPPORTED"
    r = await harness.client.post("/v1/embeddings", headers=harness.headers(), json={"input": "x"})
    assert r.status_code == 501
    r = await harness.client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "E_NOT_FOUND"
    r = await harness.client.get("/v1/chat/completions")
    assert r.status_code == 405 and r.json()["error"]["code"] == "E_SCHEMA"


# ------------------------------------------------------------------ headers


async def test_project_header_rules(harness: Harness) -> None:
    r = await harness.chat(project=None)
    assert r.status_code == 400 and r.json()["error"]["param"] == "X-Gateway-Project"
    r = await harness.chat(project=None, extra={"OpenAI-Project": "homelab-ops"})
    assert r.status_code == 200
    r = await harness.chat(project="homelab-ops", extra={"OpenAI-Project": "gateway-smoke"})
    assert r.status_code == 400
    unknown = await harness.chat(project="does-not-exist")
    ungranted = await harness.chat("n8n-executor", project="homelab-ops", cap="0.05", extra={"X-Gateway-Job-Id": "j"})
    assert unknown.status_code == ungranted.status_code == 403
    assert unknown.json()["error"]["message"] == ungranted.json()["error"]["message"]
    assert harness.upstream.attempts == 1


@pytest.mark.parametrize(
    ("caller", "cap", "code"),
    [
        ("operator", None, "E_BUDGET_CAP_MISSING"),
        ("operator", "", "E_BUDGET_CAP_MISSING"),
        ("operator", "abc", "E_BUDGET_CAP_INVALID"),
        ("operator", "-1", "E_BUDGET_CAP_INVALID"),
        ("operator", "0.0000001", "E_BUDGET_CAP_INVALID"),
        ("operator", "NaN", "E_BUDGET_CAP_INVALID"),
        ("operator", "5.01", "E_BUDGET_CAP_INVALID"),  # caller max_request_cap_usd 5.00
        ("worker-x", "1.01", "E_BUDGET_CAP_INVALID"),
    ],
)
async def test_cap_header_rules(harness: Harness, caller: str, cap: str | None, code: str) -> None:
    r = await harness.chat(caller, cap=cap)
    assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert harness.upstream.attempts == 0 and harness.ledger.in_flight() == (0, None)


async def test_project_request_ceiling_binds_the_operator_too(harness: Harness) -> None:
    # operator may present up to 5.00, but gateway-smoke's max_request_cap_usd is 0.10 (tightest wins)
    r = await harness.chat(project="gateway-smoke", cap="0.11")
    assert r.status_code == 400 and r.json()["error"]["code"] == "E_BUDGET_CAP_INVALID"
    assert "0.100000" in r.json()["error"]["message"]
    r = await harness.chat(project="gateway-smoke", cap="0.10")
    assert r.status_code == 200


async def test_executor_needs_a_job_id_and_the_grammar_holds(harness: Harness) -> None:
    r = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.05")
    assert r.status_code == 403 and "X-Gateway-Job-Id" in r.json()["error"]["message"]
    r = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.05", extra={"X-Gateway-Job-Id": "bad/id"})
    assert r.status_code == 400 and r.json()["error"]["param"] == "X-Gateway-Job-Id"
    r = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.11", extra={"X-Gateway-Job-Id": "ok"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "E_BUDGET_CAP_INVALID"
    r = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.05", extra={"X-Gateway-Job-Id": "ok"})
    assert r.status_code == 200 and r.headers["x-gateway-job-spent-usd"] == r.headers["x-gateway-list-usd"]


async def test_timeout_header_rules(harness: Harness) -> None:
    for bad in ("0", "601", "x"):
        r = await harness.chat(extra={"X-Gateway-Timeout-S": bad})
        assert r.status_code == 400 and r.json()["error"]["param"] == "X-Gateway-Timeout-S"
    assert (await harness.chat(extra={"X-Gateway-Timeout-S": "600"})).status_code == 200


async def test_lane_header_rules_and_the_deferred_lane(harness: Harness) -> None:
    r = await harness.chat(extra={"X-Gateway-Lane": "auto"})
    assert r.status_code == 400 and "auto" in r.json()["error"]["message"]
    r = await harness.chat(extra={"X-Gateway-Lane": "local"})
    assert r.status_code == 400
    r = await harness.chat(extra={"X-Gateway-Fallback": "subscription"})
    assert r.status_code == 400
    # Not granted the subscription lane at all: 403, not 503.
    r = await harness.chat(extra={"X-Gateway-Lane": "subscription"})
    assert r.status_code == 403
    before = dump(harness.ledger)
    sub_body = {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 10}  # project default: sonnet
    # Pinned to the deferred lane: 503 with Retry-After, no reservation, never spend.
    r = await harness.chat(
        "sub-operator",
        project="sub-only",
        body=sub_body,
        extra={"X-Gateway-Lane": "subscription", "X-Gateway-Fallback": "metered"},
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "E_LANE_UNAVAILABLE"
    assert r.headers["retry-after"] == "3600" and r.headers["x-should-retry"] == "true"
    # Project default is subscription, no consent: 503.
    r = await harness.chat("sub-operator", project="sub-fallback", body=sub_body)
    assert r.status_code == 503
    # Header without the project bit: 503.
    r = await harness.chat("sub-operator", project="sub-only", body=sub_body, extra={"X-Gateway-Fallback": "metered"})
    assert r.status_code == 503
    assert dump(harness.ledger) == before and harness.upstream.attempts == 0
    # Header AND bit: metered, recorded as a fallback.
    r = await harness.chat(
        "sub-operator", project="sub-fallback", body=sub_body, extra={"X-Gateway-Fallback": "metered"}
    )
    assert r.status_code == 200
    assert r.headers["x-gateway-fallback-from"] == "subscription" and r.headers["x-gateway-lane-used"] == "metered"
    rec = row(harness, r.headers["x-gateway-request-id"])
    assert rec["fallback"] == 1 and rec["lane_requested"] == "subscription" and rec["lane_used"] == "metered"
    text = (await harness.client.get("/metrics")).text
    assert 'gateway_fallback_total{from="subscription",project="sub-fallback",to="metered"} 1.0' in text


# ------------------------------------------------------------------ the product


async def test_happy_path_headers_body_row_and_log(harness: Harness) -> None:
    harness.upstream.push(
        FakeReply(text="pong", input_tokens=12, output_tokens=3, cache_read_tokens=4, model="claude-haiku-4-5-x")
    )
    r = await harness.chat(
        body={"model": "haiku", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5, "temperature": 0.1}
    )
    assert r.status_code == 200, r.text
    request_id = r.headers["x-gateway-request-id"]
    assert len(request_id) == 26
    assert r.headers["x-gateway-lane-used"] == "metered"
    # 12 in * $1 + 3 out * $5 + 4 cache read * $0.10 -> 12 + 15 + 1 = 28 micro-USD
    assert r.headers["x-gateway-billed-usd"] == "0.000028" and r.headers["x-gateway-list-usd"] == "0.000028"
    assert r.headers["x-gateway-ignored"] == "temperature"
    assert "x-gateway-job-spent-usd" not in r.headers
    remaining = float(r.headers["x-gateway-project-remaining-usd"])
    assert 19.99 < remaining < 20.0
    body = r.json()
    assert body["id"] == f"chatcmpl-{request_id}" and body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "pong" and body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 16,
        "completion_tokens": 3,
        "total_tokens": 19,
        "prompt_tokens_details": {"cached_tokens": 4},
    }
    assert body["model"] == "claude-haiku-4-5-x"
    assert body["gateway"]["request_id"] == request_id and body["gateway"]["billed_usd"] == 2.8e-05
    assert body["gateway"]["ignored"] == ["temperature"]
    rec = row(harness, request_id)
    assert rec["state"] == "settled" and rec["settled_micro"] == 28 and rec["list_micro"] == 28
    assert rec["model"] == "claude-haiku-4-5" and rec["model_used"] == "claude-haiku-4-5-x"
    assert rec["input_tokens"] == 12 and rec["cache_read_tokens"] == 4 and rec["upstream_started"] == 1
    assert rec["provider_request_id"] == "req_fake" and rec["max_tokens"] == 5 and rec["class"] == "operator"
    # worst case: count_tokens = bytes//4 = 1 -> ceil(1.05)+32 = 34 in -> 34 + 5*5 = 59 micro
    assert rec["reserved_micro"] == 59 and rec["list_reserved_micro"] == 59
    assert rec["cap_presented_micro"] == 1 * USD
    # Nothing in the row or the log could hold text.
    assert "ping" not in json.dumps(rec) and "pong" not in json.dumps(rec)
    assert all("ping" not in json.dumps(line) and "pong" not in json.dumps(line) for line in harness.log.lines)
    assert harness.log.events("reserve") and harness.log.events("settle")
    text = (await harness.client.get("/metrics")).text
    assert 'gateway_requests_total{lane="metered",outcome="ok",project="homelab-ops"} 1.0' in text
    assert 'gateway_tokens_total{kind="cache_read",lane="metered",model="claude-haiku-4-5"} 4.0' in text


async def test_settled_recomputes_exactly_from_token_counts(harness: Harness) -> None:
    harness.upstream.push(
        FakeReply(
            input_tokens=1234,
            output_tokens=77,
            cache_write_5m_tokens=100,
            cache_write_1h_tokens=3,
            cache_read_tokens=999,
        ),
        FakeReply(input_tokens=1, output_tokens=1),
        FakeReply(input_tokens=0, output_tokens=0),
    )
    for _ in range(3):
        assert (
            await harness.chat(body={"model": "opus", "messages": [{"role": "user", "content": "x"}], "max_tokens": 10})
        ).status_code == 200
    registry = harness.state.registry
    conn: sqlite3.Connection = harness.ledger._conn
    for rec in conn.execute("SELECT * FROM requests WHERE state = 'settled'"):
        usage = TokenUsage(
            input_tokens=rec["input_tokens"],
            output_tokens=rec["output_tokens"],
            cache_write_5m_tokens=rec["cache_write_5m_tokens"],
            cache_write_1h_tokens=rec["cache_write_1h_tokens"],
            cache_read_tokens=rec["cache_read_tokens"],
        )
        prices = registry.models[rec["model"]].prices
        assert rec["settled_micro"] == usage_cost_micro(usage, prices, harness.config.billed_price_multiplier_pct)
        assert rec["list_micro"] == usage_cost_micro(usage, prices)


async def test_cap_zero_is_402_with_no_row_and_no_call(harness: Harness) -> None:
    before = dump(harness.ledger)
    r = await harness.chat(cap="0")
    assert r.status_code == 402, r.text
    err = r.json()["error"]
    assert err["code"] == "E_BUDGET_EXCEEDED" and err["type"] == "insufficient_quota" and err["retryable"] is False
    assert err["scope"] == "request" and err["affordable_max_tokens"] == 0 and err["remaining_usd"] == 0
    assert err["would_reserve_usd"] > 0
    assert r.headers["x-should-retry"] == "false"
    assert dump(harness.ledger) == before
    assert harness.upstream.attempts == 0 and harness.upstream.count_tokens_calls == 1
    text = (await harness.client.get("/metrics")).text
    assert 'gateway_requests_total{lane="metered",outcome="refused_cap",project="homelab-ops"} 1.0' in text
    assert 'gateway_refusals_total{scope="request"} 1.0' in text


async def test_request_cap_refusal_names_affordable_tokens(harness: Harness) -> None:
    # opus: max 16000 out * $25 = $0.40 + input; cap $0.10 -> refused with an affordable count
    r = await harness.chat(cap="0.10", body={"model": "opus", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["scope"] == "request" and 3_000 < err["affordable_max_tokens"] < 4_000
    r = await harness.chat(
        cap="0.10",
        body={
            "model": "opus",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": err["affordable_max_tokens"],
        },
    )
    assert r.status_code == 200


async def test_streaming_is_buffered(harness: Harness) -> None:
    harness.upstream.push(FakeReply(text="streamed", input_tokens=3, output_tokens=2))
    body = {
        "model": "haiku",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    async with harness.client.stream("POST", "/v1/chat/completions", headers=harness.headers(), json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-gateway-stream"] == "buffered"
        request_id = r.headers["x-gateway-request-id"]
        assert "x-gateway-billed-usd" not in r.headers  # money rides the final chunk
        lines = [line async for line in r.aiter_lines() if line.startswith("data: ")]
    assert len(lines) == 3 and lines[-1] == "data: [DONE]"
    first = json.loads(lines[0][6:])
    last = json.loads(lines[1][6:])
    assert first["object"] == "chat.completion.chunk" and first["choices"][0]["delta"]["content"] == "streamed"
    assert last["choices"][0]["finish_reason"] == "stop" and last["usage"]["completion_tokens"] == 2
    assert last["gateway"]["request_id"] == request_id and last["gateway"]["billed_usd"] == 1.3e-05
    assert row(harness, request_id)["state"] == "settled"


async def test_streaming_error_after_headers_is_an_error_event(harness: Harness) -> None:
    harness.upstream.push(FakeFailure("server", status=500))
    body = {"model": "haiku", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5, "stream": True}
    async with harness.client.stream("POST", "/v1/chat/completions", headers=harness.headers(), json=body) as r:
        assert r.status_code == 200
        request_id = r.headers["x-gateway-request-id"]
        lines = [line async for line in r.aiter_lines() if line.startswith("data: ")]
    assert json.loads(lines[0][6:])["error"]["code"] == "E_UPSTREAM_ERROR" and lines[-1] == "data: [DONE]"
    assert row(harness, request_id)["state"] == "released"


async def test_job_flow_pin_spent_and_ledger_reads(harness: Harness) -> None:
    harness.upstream.push(FakeReply(input_tokens=100, output_tokens=10), FakeReply(input_tokens=200, output_tokens=20))
    job = {"X-Gateway-Job-Id": "01JOB"}
    a = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.05", extra=job)
    b = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.05", extra=job)
    assert a.status_code == b.status_code == 200
    assert a.headers["x-gateway-job-spent-usd"] == "0.000150"  # 100 + 50
    assert b.headers["x-gateway-job-spent-usd"] == "0.000450"  # + 200 + 100
    c = await harness.chat("n8n-executor", project="gateway-smoke", cap="0.06", extra=job)
    assert c.status_code == 409 and c.json()["error"]["code"] == "E_JOB_CAP_MISMATCH"
    assert c.headers["x-should-retry"] == "false" and c.json()["error"]["pinned_cap_usd"] == 0.05
    read = await harness.client.get("/ledger/jobs/01JOB", headers=harness.headers("n8n-executor"))
    assert read.status_code == 200
    assert read.json()["spent_usd"] == 0.00045 and read.json()["cap_usd"] == 0.05 and read.json()["requests"] == 2
    # A different caller's job with the same id is a different scope: 404 for the operator.
    assert (await harness.client.get("/ledger/jobs/01JOB", headers=harness.headers("operator"))).status_code == 404
    assert (await harness.client.get("/ledger/jobs/nope", headers=harness.headers("n8n-executor"))).status_code == 404


async def test_ledger_reads_and_brake_reset(harness: Harness) -> None:
    assert (await harness.chat()).status_code == 200
    r = await harness.client.get("/ledger/projects/homelab-ops", headers=harness.headers())
    assert r.status_code == 200 and r.json()["cap_usd"] == 20.0 and r.json()["settled_billed_usd"] > 0
    assert (await harness.client.get("/ledger/projects/sub-only", headers=harness.headers())).status_code == 403
    r = await harness.client.get("/ledger/requests", params={"project": "homelab-ops"}, headers=harness.headers())
    assert r.status_code == 200 and len(r.json()["requests"]) == 1
    rec = r.json()["requests"][0]
    assert rec["state"] == "settled" and "settled_usd" in rec and "reserved_usd" in rec
    assert (await harness.client.get("/ledger/requests", headers=harness.headers())).status_code == 400
    assert (
        await harness.client.get(
            "/ledger/requests", params={"project": "homelab-ops", "since": "x"}, headers=harness.headers()
        )
    ).status_code == 400
    r = await harness.client.get("/ledger/lanes", headers=harness.headers())
    assert r.status_code == 200 and r.json()["lanes"][0]["up"] is True and r.json()["brakes"][0]["tripped"] is False
    r = await harness.client.post("/ledger/brake-reset", headers=harness.headers(), json={"lane": "metered"})
    assert r.status_code == 400 and r.json()["error"]["param"] == "reason"
    r = await harness.client.post(
        "/ledger/brake-reset", headers=harness.headers(), json={"lane": "metered", "reason": "test"}
    )
    assert r.status_code == 200 and r.json()["reset_by"] == "operator"
    assert harness.log.events("brake_reset")


async def test_models_endpoint(harness: Harness) -> None:
    r = await harness.client.get("/v1/models", headers=harness.headers("n8n-executor", project=None, cap=None))
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["claude-haiku-4-5"]
    assert r.json()["data"][0]["aliases"] == ["haiku"] and r.json()["data"][0]["projects"] == ["gateway-smoke"]
    r = await harness.client.get("/v1/models", headers=harness.headers("operator", project=None, cap=None))
    assert {m["id"] for m in r.json()["data"]} == {"claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"}
    r = await harness.client.get("/v1/models", headers=harness.headers("operator", project="gateway-smoke", cap=None))
    assert [m["id"] for m in r.json()["data"]] == ["claude-haiku-4-5"]
    assert (
        await harness.client.get("/v1/models", headers=harness.headers("operator", project="sub-only", cap=None))
    ).status_code == 403


async def test_body_limit_and_invalid_json(harness: Harness) -> None:
    big = b"x" * (harness.config.body_limit_bytes + 1)
    r = await harness.client.post(
        "/v1/chat/completions", headers={**harness.headers(), "content-type": "application/json"}, content=big
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "E_SCHEMA"
    r = await harness.client.post(
        "/v1/chat/completions", headers={**harness.headers(), "content-type": "application/json"}, content=b"{not json"
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "E_SCHEMA"
    r = await harness.client.post("/v1/chat/completions", headers=harness.headers(), content=b"")
    assert r.status_code == 400
    assert harness.upstream.attempts == 0


# ------------------------------------------------------------------ upstream outcomes


@pytest.mark.parametrize(
    ("failure", "status", "code", "state", "retryable"),
    [
        (FakeFailure("rate_limited", status=429, retry_after_s=30), 429, "E_UPSTREAM_RATE_LIMITED", "released", True),
        (FakeFailure("spend_limit", status=429), 503, "E_LANE_UNAVAILABLE", "released", True),
        (FakeFailure("auth", status=401), 502, "E_UPSTREAM_AUTH", "released", False),
        (FakeFailure("rejected", status=400), 502, "E_UPSTREAM_ERROR", "released", False),
        (FakeFailure("server", status=529), 502, "E_UPSTREAM_ERROR", "released", True),
        (FakeFailure("server", status=500, started_before_failure=True), 502, "E_UPSTREAM_ERROR", "aborted", False),
        (FakeFailure("network", before_generation=False), 502, "E_UPSTREAM_ERROR", "aborted", False),  # severed body
        (FakeFailure("bad_response"), 502, "E_UPSTREAM_ERROR", "aborted", False),
        (FakeFailure("timeout"), 504, "E_TIMEOUT", "timeout", False),
    ],
)
async def test_upstream_failure_mapping(
    tmp_path: Path, failure: FakeFailure, status: int, code: str, state: str, retryable: bool
) -> None:
    async with harness_ctx(tmp_path, upstream=FakeMeteredClient(script=[failure])) as h:
        r = await h.chat()
        assert r.status_code == status, r.text
        err = r.json()["error"]
        assert err["code"] == code and err["retryable"] is retryable
        assert r.headers["x-should-retry"] == ("true" if retryable else "false")
        rec = h.ledger.requests_for_project("homelab-ops", 0)[0]
        assert rec["state"] == state and rec["error_code"] == code and rec["http_status"] == status
        if state == "released":
            assert rec["settled_micro"] == 0 and rec["upstream_started"] == 0
        else:
            assert rec["settled_micro"] == rec["reserved_micro"] and rec["list_micro"] == rec["list_reserved_micro"]
        if failure.kind == "rate_limited":
            assert r.headers["retry-after"] == "30" and h.state.lane.status().up is True
        if failure.kind == "spend_limit":
            assert h.state.lane.status().cooling_until is not None and "retry-after" in r.headers
        if failure.kind == "auth":
            assert h.state.lane.status().auth_ok is False
            assert (await h.chat()).status_code == 503  # lane down within one request
        assert h.ledger.recompute_totals() == []


async def test_retry_once_paths(tmp_path: Path) -> None:
    async with harness_ctx(
        tmp_path,
        upstream=FakeMeteredClient(script=[FakeFailure("rate_limited", status=429, retry_after_s=0.01), FakeReply()]),
    ) as h:
        r = await h.chat()
        assert r.status_code == 200 and h.upstream.attempts == 2
        assert len(h.ledger.requests_for_project("homelab-ops", 0)) == 1  # one row, one reservation
        assert h.log.events("upstream_retry")
    async with harness_ctx(
        tmp_path / "b", upstream=FakeMeteredClient(script=[FakeFailure("network"), FakeReply()])
    ) as h:
        r = await h.chat()  # connection refused before the body: retried once
        assert r.status_code == 200 and h.upstream.attempts == 2
    async with harness_ctx(
        tmp_path / "b2",
        upstream=FakeMeteredClient(script=[FakeFailure("network", before_generation=False), FakeReply()]),
    ) as h:
        r = await h.chat()  # severed after the body: never retried, settled at the reservation
        assert r.status_code == 502 and h.upstream.attempts == 1
    async with harness_ctx(
        tmp_path / "c",
        upstream=FakeMeteredClient(script=[FakeFailure("rate_limited", status=429, retry_after_s=30), FakeReply()]),
    ) as h:
        r = await h.chat()
        assert r.status_code == 429 and h.upstream.attempts == 1  # Retry-After > 10 s: passed through
    async with harness_ctx(
        tmp_path / "d", upstream=FakeMeteredClient(script=[FakeFailure("timeout"), FakeReply()])
    ) as h:
        r = await h.chat()
        assert r.status_code == 504 and h.upstream.attempts == 1


async def test_timeouts_settle_at_the_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "TIMEOUT_MARGIN_S", 0)
    for started in (True, False):
        async with harness_ctx(
            tmp_path / str(started), upstream=FakeMeteredClient(script=[FakeHang(started=started)])
        ) as h:
            r = await h.chat(extra={"X-Gateway-Timeout-S": "1"})
            assert r.status_code == 504 and r.json()["error"]["code"] == "E_TIMEOUT"
            rec = h.ledger.requests_for_project("homelab-ops", 0)[0]
            assert rec["state"] == "timeout" and rec["settled_micro"] == rec["reserved_micro"]
            assert rec["upstream_started"] == (1 if started else 0)
            assert h.ledger.recompute_totals() == []


async def test_generations_equal_rows_with_upstream_started(tmp_path: Path) -> None:
    script: list[Outcome] = [
        FakeReply(),
        FakeFailure("server", status=500),
        FakeFailure("server", status=500, started_before_failure=True),
        FakeReply(),
        FakeFailure("rate_limited", status=429, retry_after_s=30),
    ]
    async with harness_ctx(tmp_path, upstream=FakeMeteredClient(script=script)) as h:
        for _ in script:
            await h.chat()
        await h.chat(cap="0")  # refused: no call
        conn: sqlite3.Connection = h.ledger._conn
        started_rows = conn.execute("SELECT COUNT(*) FROM requests WHERE upstream_started = 1").fetchone()[0]
        assert started_rows == h.upstream.generations == 3
        assert h.upstream.attempts == 5
        released = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE state = 'released' AND upstream_started = 0"
        ).fetchone()[0]
        assert released == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM requests WHERE state = 'released' AND upstream_started = 1").fetchone()[
                0
            ]
            == 0
        )


async def test_lane_goes_down_after_three_failures_and_refuses_without_reserving(tmp_path: Path) -> None:
    clock = FakeClock()
    upstream = FakeMeteredClient(script=[FakeFailure("server", status=500)] * 3 + [FakeReply()])
    async with harness_ctx(tmp_path, upstream=upstream, clock=clock) as h:
        for _ in range(3):
            assert (await h.chat()).status_code == 502
        before = dump(h.ledger)
        r = await h.chat()
        assert r.status_code == 503 and r.json()["error"]["code"] == "E_LANE_UNAVAILABLE"
        assert r.headers["retry-after"] == "2" and dump(h.ledger) == before and h.upstream.attempts == 3
        lanes = (await h.client.get("/ledger/lanes", headers=h.headers())).json()
        assert lanes["lanes"][0]["up"] is False
        assert 'gateway_lane_up{lane="metered"} 0.0' in (await h.client.get("/metrics")).text
        assert h.log.events("lane_down")
        clock.advance(2_500)  # past the backoff: half-open, the next request tries and succeeds
        assert (await h.chat()).status_code == 200
        assert h.state.lane.status().up is True and h.log.events("lane_up")
        assert (await h.client.get("/readyz")).status_code == 200  # lane health is in neither probe


async def test_count_tokens_outage_falls_back_to_the_heuristic(tmp_path: Path) -> None:
    upstream = FakeMeteredClient(count_tokens_failure=UpstreamFailure("server", status=500, before_generation=True))
    async with harness_ctx(tmp_path, upstream=upstream) as h:
        r = await h.chat(
            body={"model": "haiku", "messages": [{"role": "user", "content": "0123456789"}], "max_tokens": 5}
        )
        assert r.status_code == 200
        rec = h.ledger.requests_for_project("homelab-ops", 0)[0]
        # ceil(10 bytes / 2.5) = 4 in tokens -> 4 + 25 = 29 micro
        assert rec["reserved_micro"] == 29
        assert h.log.events("count_tokens_fallback")
        assert (
            'gateway_upstream_errors_total{kind="count_tokens",lane="metered"} 1.0'
            in (await h.client.get("/metrics")).text
        )


# ------------------------------------------------------------------ scopes through HTTP


async def test_brake_through_http(tmp_path: Path) -> None:
    async with harness_ctx(tmp_path, env={"GATEWAY_METERED_DAILY_CEILING_USD": "0.10"}) as h:
        big = {
            "model": "opus",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 3000,
        }  # ~$0.075 worst case
        h.upstream.push(FakeReply(input_tokens=10, output_tokens=3000))  # settles at ~$0.075 too
        assert (await h.chat(cap="0.10", body=big)).status_code == 200
        r = await h.chat(cap="0.10", body=big)
        assert r.status_code == 402 and r.json()["error"]["scope"] == "brake_metered"
        assert 'gateway_brake_tripped{lane="metered"} 1.0' in (await h.client.get("/metrics")).text
        assert (await h.chat()).status_code == 402  # latched
        assert h.log.events("brake_tripped")
        r = await h.client.post(
            "/ledger/brake-reset", headers=h.headers(), json={"lane": "metered", "reason": "offender cancelled"}
        )
        assert r.status_code == 200 and r.json()["tripped"] is False
        assert (await h.chat()).status_code == 200


async def test_caller_day_and_project_period_scopes_through_http(tmp_path: Path) -> None:
    yaml_day = REGISTRY_YAML.replace("max_day_billed_usd: 5.00", "max_day_billed_usd: 0.00005")
    async with harness_ctx(tmp_path, registry_yaml=yaml_day) as h:
        r = await h.chat()
        assert r.status_code == 402 and r.json()["error"]["scope"] == "caller_day"
    # gateway-smoke: period cap 100 micro-USD, per-request ceiling 50 micro-USD.
    yaml_project = REGISTRY_YAML.replace("cap_usd: 1.00,", "cap_usd: 0.0001,").replace(
        "max_request_cap_usd: 0.10", "max_request_cap_usd: 0.00005"
    )
    async with harness_ctx(tmp_path / "p", registry_yaml=yaml_project) as h:
        # The executor's job cap (50) cannot cover haiku's default 100 output tokens: scope job.
        r = await h.chat("n8n-executor", project="gateway-smoke", cap="0.00005", extra={"X-Gateway-Job-Id": "j"})
        assert r.status_code == 402 and r.json()["error"]["scope"] == "job"
        # Each tiny call reserves 39 and settles 35: the third cannot fit under the period cap of 100.
        tiny = {"model": "haiku", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1}
        for _ in range(2):
            assert (await h.chat("operator", project="gateway-smoke", cap="0.00005", body=tiny)).status_code == 200
        r = await h.chat("operator", project="gateway-smoke", cap="0.00005", body=tiny)
        assert r.status_code == 402 and r.json()["error"]["scope"] == "project_period"
        assert r.json()["error"]["remaining_usd"] == 3e-05


# ------------------------------------------------------------------ boot and readiness


async def test_registry_failure_degrades_to_not_ready(tmp_path: Path) -> None:
    async with harness_ctx(tmp_path, registry_yaml="projects: []\n") as h:
        assert (await h.client.get("/healthz")).status_code == 200
        ready = await h.client.get("/readyz")
        assert ready.status_code == 503 and "registry" in ready.json()["reason"]
        r = await h.chat()
        assert r.status_code == 503 and r.json()["error"]["code"] == "E_LEDGER_UNAVAILABLE"
        assert h.log.events("registry_parse_failed")


async def test_boot_sweeps_orphans_and_a_totals_mismatch_blocks_money(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "gateway.db"
    seed = Ledger(open_db(str(path)), now_ms=clock.now_ms, log=null_log)
    seed.reserve(make_reserve_input())
    seed.reserve(make_reserve_input(project_id="gateway-smoke"))
    clock.advance(1_000)
    async with harness_ctx(tmp_path, clock=clock) as h:
        assert h.log.events("sweep_done")[0]["count"] == 2
        text = (await h.client.get("/metrics")).text
        assert 'gateway_swept_usd_total{project="homelab-ops"} 0.0006' in text
        assert (await h.client.get("/readyz")).status_code == 200
        assert h.ledger.in_flight() == (0, None)
        conn: sqlite3.Connection = h.ledger._conn
        conn.execute("UPDATE scope_totals SET settled_micro = settled_micro + 7 WHERE scope_kind = 'project'")
    clock.advance(1_000)
    async with harness_ctx(tmp_path, clock=clock) as h:
        ready = await h.client.get("/readyz")
        assert ready.status_code == 503 and "mismatch" in ready.json()["reason"]
        r = await h.chat()
        assert r.status_code == 503 and r.json()["error"]["code"] == "E_LEDGER_UNAVAILABLE" and h.upstream.attempts == 0
        assert (await h.client.get("/healthz")).status_code == 200
        assert (await h.client.get("/v1/models", headers=h.headers(project=None, cap=None))).status_code == 200


async def test_clock_guard_blocks_readiness_and_money(harness: Harness) -> None:
    assert (await harness.chat()).status_code == 200
    harness.clock.advance(-61_000)
    ready = await harness.client.get("/readyz")
    assert ready.status_code == 503 and "clock" in ready.json()["reason"]
    r = await harness.chat()
    assert r.status_code == 503 and harness.upstream.attempts == 1
    harness.clock.advance(120_000)
    assert (await harness.client.get("/readyz")).status_code == 200
    assert (await harness.chat()).status_code == 200


async def test_write_failure_after_the_call_leaves_the_row_for_the_sweep(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn: sqlite3.Connection = harness.ledger._conn
    original = harness.ledger.settle

    def broken_settle(*args: Any, **kwargs: Any) -> SettleResult:
        conn.execute("DROP TABLE scope_totals")
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.ledger, "settle", broken_settle)
    r = await harness.chat()
    assert r.status_code == 503 and r.json()["error"]["code"] == "E_LEDGER_UNAVAILABLE"
    assert harness.log.events("settle_write_failed")
    assert harness.ledger.in_flight()[0] == 1  # swept at the next boot, over-counting
