"""Shared fixtures: a controllable clock, a test registry, the ASGI harness with a fake upstream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gateway.config import Config, load_config
from gateway.db import open_db
from gateway.fake_upstream import FakeMeteredClient
from gateway.http import AppState, build_app
from gateway.ledger import Ledger, ReserveInput
from gateway.main import boot
from gateway.money import Prices, component_micro
from gateway.registry import Registry, parse_registry

HAIKU = Prices(
    input=1_000_000, output=5_000_000, cache_write_5m=1_250_000, cache_write_1h=2_000_000, cache_read=100_000
)
USD = 1_000_000

TOKENS: dict[str, str] = {
    "operator": "operator-token-0123456789",
    "sub-operator": "sub-operator-token-0123456789",
    "n8n-executor": "executor-token-0123456789",
    "worker-x": "worker-token-0123456789",
    "worker-01": "lane-agent-token-0123456789",
    # In the token map but NOT in the registry: must authenticate nobody.
    "ghost": "ghost-token-0123456789",
}

REGISTRY_YAML = """
prices_as_of: 2026-09-01
console_workspace_limit_usd: 100
models:
  claude-opus-5:    {input: 5.00, output: 25.00, cache_write_5m: 6.25, cache_write_1h: 10.00, cache_read: 0.50, max_tokens: 16000, default_max_tokens: 4096, effort: true,  lanes: [subscription, metered], cli_alias: opus}
  claude-sonnet-5:  {input: 2.00, output: 10.00, cache_write_5m: 2.50, cache_write_1h: 4.00,  cache_read: 0.20, max_tokens: 16000, default_max_tokens: 4096, effort: true,  lanes: [subscription, metered], cli_alias: sonnet}
  claude-haiku-4-5: {input: 1.00, output: 5.00,  cache_write_5m: 1.25, cache_write_1h: 2.00,  cache_read: 0.10, max_tokens: 8000,  default_max_tokens: 1024, effort: false, lanes: [metered]}
aliases: {opus: claude-opus-5, sonnet: claude-sonnet-5, haiku: claude-haiku-4-5}
projects:
  homelab-ops:   {cap_usd: 20.00, period: month, lanes: [metered], default_lane: metered, fallback_allowed: false, max_request_cap_usd: 5.00, default_model: claude-sonnet-5, models: [claude-sonnet-5, claude-opus-5, claude-haiku-4-5]}
  gateway-smoke: {cap_usd: 1.00,  period: month, lanes: [metered], default_lane: metered, fallback_allowed: false, max_request_cap_usd: 0.10, default_model: claude-haiku-4-5, models: [claude-haiku-4-5]}
  sub-fallback:  {cap_usd: 2.00,  period: month, lanes: [subscription, metered], default_lane: subscription, fallback_allowed: true,  max_request_cap_usd: 1.00, default_model: claude-sonnet-5, models: [claude-sonnet-5]}
  sub-only:      {cap_usd: 2.00,  period: month, lanes: [subscription, metered], default_lane: subscription, fallback_allowed: false, max_request_cap_usd: 1.00, default_model: claude-sonnet-5, models: [claude-sonnet-5]}
callers:
  operator:      {class: operator,   projects: [homelab-ops, gateway-smoke], lanes: [metered], max_request_cap_usd: 5.00, max_day_billed_usd: 5.00}
  sub-operator:  {class: operator,   projects: [sub-fallback, sub-only],     lanes: [metered, subscription], max_request_cap_usd: 1.00}
  n8n-executor:  {class: executor,   projects: [gateway-smoke],              lanes: [metered], max_request_cap_usd: 0.10}
  worker-x:      {class: worker,     projects: [homelab-ops],                lanes: [metered], max_request_cap_usd: 1.00}
  worker-01:     {class: lane-agent, lane: subscription}
"""

T0 = int(datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


class FakeClock:
    def __init__(self, start_ms: int = T0) -> None:
        self.value = start_ms

    def now_ms(self) -> int:
        return self.value

    def advance(self, ms: int) -> None:
        self.value += ms

    def set(self, ms: int) -> None:
        self.value = ms


class LogCapture:
    def __init__(self) -> None:
        self.lines: list[dict[str, object]] = []

    def __call__(self, evt: str, /, **fields: object) -> None:
        self.lines.append({"evt": evt, **fields})

    def events(self, evt: str) -> list[dict[str, object]]:
        return [line for line in self.lines if line["evt"] == evt]


def load_test_registry() -> Registry:
    return parse_registry(REGISTRY_YAML)


def base_env(tmp_path: Path, registry_yaml: str = REGISTRY_YAML, **overrides: str) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(registry_yaml)
    env = {
        "DB_PATH": str(tmp_path / "gateway.db"),
        "REGISTRY_PATH": str(registry_path),
        "GATEWAY_CALLER_TOKENS": json.dumps(TOKENS),
        "ANTHROPIC_API_KEY": "sk-ant-test-never-used",
        "LANE_PROBE_INTERVAL_MS": "0",
    }
    env.update(overrides)
    return env


def make_config(tmp_path: Path, **overrides: str) -> Config:
    return load_config(base_env(tmp_path, **overrides))


def make_reserve_input(**overrides: object) -> ReserveInput:
    in_tokens = overrides.pop("in_tokens", 100)
    max_tokens = overrides.pop("max_tokens", 100)
    prices = overrides.pop("prices", HAIKU)
    multiplier = overrides.pop("billed_multiplier_pct", 100)
    assert isinstance(in_tokens, int) and isinstance(max_tokens, int) and isinstance(multiplier, int)
    assert isinstance(prices, Prices)
    in_cost_list = component_micro(in_tokens, prices.input)
    in_cost_billed = component_micro(in_tokens, prices.input, multiplier)
    values: dict[str, object] = {
        "caller_id": "operator",
        "caller_class": "operator",
        "project_id": "homelab-ops",
        "job_id": None,
        "lane_requested": "metered",
        "lane_used": "metered",
        "fallback": False,
        "model": "claude-haiku-4-5",
        "cap_presented_micro": USD,
        "reserved_micro": in_cost_billed + component_micro(max_tokens, prices.output, multiplier),
        "list_reserved_micro": in_cost_list + component_micro(max_tokens, prices.output),
        "max_tokens": max_tokens,
        "in_tokens": in_tokens,
        "in_cost_billed_micro": in_cost_billed,
        "in_cost_list_micro": in_cost_list,
        "output_price_micro_per_mtok": prices.output,
        "billed_multiplier_pct": multiplier,
        "project_cap_micro": 20 * USD,
        "caller_day_cap_micro": None,
        "brake_cap_micro": 5 * USD,
    }
    values.update(overrides)
    return ReserveInput(**values)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def log() -> LogCapture:
    return LogCapture()


@pytest.fixture
def ledger(clock: FakeClock, log: LogCapture) -> Ledger:
    return Ledger(open_db(":memory:"), now_ms=clock.now_ms, log=log)


@dataclass
class Harness:
    state: AppState
    client: httpx.AsyncClient
    clock: FakeClock
    upstream: FakeMeteredClient
    log: LogCapture
    config: Config
    tokens: Mapping[str, str] = field(default_factory=lambda: TOKENS)

    def headers(
        self,
        caller: str = "operator",
        *,
        project: str | None = "homelab-ops",
        cap: str | None = "1.00",
        **extra: str,
    ) -> dict[str, str]:
        out = {"Authorization": f"Bearer {self.tokens[caller]}"}
        if project is not None:
            out["X-Gateway-Project"] = project
        if cap is not None:
            out["X-Gateway-Budget-Cap-USD"] = cap
        out.update(extra)
        return out

    @property
    def ledger(self) -> Ledger:
        return self.state.ledger

    async def chat(
        self,
        caller: str = "operator",
        *,
        project: str | None = "homelab-ops",
        cap: str | None = "1.00",
        body: Mapping[str, object] | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        payload: dict[str, object] = {
            "model": "haiku",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 100,
        }
        if body is not None:
            payload = dict(body)
        return await self.client.post(
            "/v1/chat/completions",
            headers=self.headers(caller, project=project, cap=cap, **dict(extra or {})),
            json=payload,
        )


async def make_harness(
    tmp_path: Path,
    *,
    registry_yaml: str = REGISTRY_YAML,
    upstream: FakeMeteredClient | None = None,
    clock: FakeClock | None = None,
    env: Mapping[str, str] | None = None,
) -> Harness:
    clock = clock or FakeClock()
    upstream = upstream or FakeMeteredClient()
    log = LogCapture()
    config = load_config(base_env(tmp_path, registry_yaml, **dict(env or {})))
    state = boot(config, upstream=upstream, log=log, now_ms=clock.now_ms)
    app = build_app(state)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://gateway"
    )
    return Harness(state=state, client=client, clock=clock, upstream=upstream, log=log, config=config)


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await make_harness(tmp_path)
    try:
        yield h
    finally:
        await h.client.aclose()
