"""``uv run gateway-smoke-local``: boot against a stub upstream, spend nothing, prove the seams.

Runs one completion at cap $0.01 against the example registry, prints the
ledger row, then shows the 402 for cap 0 (spec §14 slice 1 verify line).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
from pathlib import Path

import httpx

from gateway.config import load_config
from gateway.fake_upstream import FakeMeteredClient, FakeReply
from gateway.http import build_app
from gateway.main import boot

EXAMPLE_REGISTRY = Path(__file__).resolve().parents[2] / "registry.example.yaml"


async def run() -> int:
    operator_token = secrets.token_hex(32)
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "DB_PATH": str(Path(tmp) / "gateway.db"),
            "REGISTRY_PATH": str(EXAMPLE_REGISTRY),
            "GATEWAY_CALLER_TOKENS": json.dumps({"operator": operator_token, "n8n-executor": secrets.token_hex(32)}),
            "ANTHROPIC_API_KEY": "sk-ant-local-smoke-never-used",
            "LANE_PROBE_INTERVAL_MS": "0",
        }
        config = load_config(env)
        upstream = FakeMeteredClient(default=FakeReply(text="pong", input_tokens=12, output_tokens=3))
        state = boot(config, upstream=upstream)
        app = build_app(state)
        headers = {
            "Authorization": f"Bearer {operator_token}",
            "X-Gateway-Project": "gateway-smoke",
            "X-Gateway-Budget-Cap-USD": "0.01",
        }
        body = {"model": "haiku", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            ready = await client.get("/readyz")
            print(f"readyz: {ready.status_code} {ready.json()}")
            response = await client.post("/v1/chat/completions", headers=headers, json=body)
            print(f"completion: {response.status_code}")
            for name, value in response.headers.items():
                if name.lower().startswith("x-gateway"):
                    print(f"  {name}: {value}")
            print(json.dumps(response.json(), indent=2))
            request_id = response.headers.get("x-gateway-request-id", "")
            row = state.ledger.request_row(request_id)
            print("ledger row:")
            print(json.dumps(row, indent=2, default=str))
            zero = await client.post(
                "/v1/chat/completions", headers={**headers, "X-Gateway-Budget-Cap-USD": "0"}, json=body
            )
            print(f"cap 0: {zero.status_code} x-should-retry={zero.headers.get('x-should-retry')}")
            print(json.dumps(zero.json(), indent=2))
            ok = response.status_code == 200 and zero.status_code == 402 and upstream.attempts == 1
            print("SMOKE OK" if ok else "SMOKE FAILED")
            return 0 if ok else 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))
