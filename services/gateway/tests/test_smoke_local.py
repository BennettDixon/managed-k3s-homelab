from gateway.smoke_local import run


async def test_smoke_local_runs_green_against_the_stub_upstream(capsys) -> None:  # type: ignore[no-untyped-def]
    assert await run() == 0
    out = capsys.readouterr().out
    assert "SMOKE OK" in out
    assert "X-Gateway-Billed-USD" in out or "x-gateway-billed-usd" in out
    assert '"state": "settled"' in out
    assert "402" in out
