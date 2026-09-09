from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.money import (
    MICRO,
    MoneyError,
    Prices,
    TokenUsage,
    affordable_tokens,
    component_micro,
    heuristic_input_tokens,
    micro_to_usd_float,
    micro_to_usd_str,
    reserve_input_tokens,
    usage_cost_micro,
    usd_to_micro,
    worst_case_micro,
)

OPUS = Prices(
    input=5_000_000, output=25_000_000, cache_write_5m=6_250_000, cache_write_1h=10_000_000, cache_read=500_000
)


def test_usd_to_micro_exact_forms() -> None:
    assert usd_to_micro("0.01") == 10_000
    assert usd_to_micro("5") == 5_000_000
    assert usd_to_micro(" 0.000001 ") == 1
    assert usd_to_micro("1e-2") == 10_000
    assert usd_to_micro(6.25) == 6_250_000  # a YAML float means what the human typed
    assert usd_to_micro(0.1) == 100_000
    assert usd_to_micro(Decimal("0.5")) == 500_000
    assert usd_to_micro(0) == 0


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "Infinity", "-Infinity", "0.0000001", "1.2345678", "0x10"])
def test_usd_to_micro_rejects(bad: str) -> None:
    with pytest.raises(MoneyError):
        usd_to_micro(bad)


def test_usd_to_micro_rejects_bool() -> None:
    with pytest.raises(MoneyError):
        usd_to_micro(True)


def test_micro_to_usd_formats() -> None:
    assert micro_to_usd_str(1) == "0.000001"
    assert micro_to_usd_str(5_000_000) == "5.000000"
    assert micro_to_usd_str(0) == "0.000000"
    assert micro_to_usd_float(2_500_000) == 2.5


def test_component_micro_ceils_each_component() -> None:
    # 1 token at $0.50/MTok = 0.5 micro-USD -> ceil -> 1
    assert component_micro(1, 500_000) == 1
    assert component_micro(0, 500_000) == 0
    assert component_micro(1_000_000, 5_000_000) == 5_000_000
    # multiplier applied before the ceiling
    assert component_micro(1_000_000, 5_000_000, 110) == 5_500_000
    assert component_micro(1, 5_000_000, 110) == 6


def test_worst_case_and_usage_costs_match_spec_example() -> None:
    # haiku, max_tokens 5, ~12 input tokens: about $0.0002 per spec §14 slice 2
    haiku = Prices(
        input=1_000_000, output=5_000_000, cache_write_5m=1_250_000, cache_write_1h=2_000_000, cache_read=100_000
    )
    assert worst_case_micro(12, 5, haiku) == 12 + 25
    usage = TokenUsage(input_tokens=12, output_tokens=3, cache_read_tokens=10)
    assert usage_cost_micro(usage, haiku) == 12 + 15 + 1  # cache read 10 tokens * 0.1 -> ceil(1.0) = 1


def test_reserve_input_tokens_margin() -> None:
    assert reserve_input_tokens(0) == 32
    assert reserve_input_tokens(100) == 105 + 32
    assert reserve_input_tokens(101) == 107 + 32  # ceil(106.05)


def test_heuristic_input_tokens() -> None:
    assert heuristic_input_tokens(0) == 0
    assert heuristic_input_tokens(5) == 2
    assert heuristic_input_tokens(6) == 3


def test_negative_usage_rejected() -> None:
    with pytest.raises(MoneyError):
        TokenUsage(input_tokens=-1)


@given(
    budget=st.integers(min_value=0, max_value=10 * MICRO),
    price=st.integers(min_value=1, max_value=100 * MICRO),
    mult=st.integers(min_value=100, max_value=200),
)
def test_affordable_tokens_is_the_exact_inverse(budget: int, price: int, mult: int) -> None:
    n = affordable_tokens(budget, price, mult)
    assert component_micro(n, price, mult) <= budget
    assert component_micro(n + 1, price, mult) > budget


@given(
    tokens=st.integers(min_value=0, max_value=1_000_000),
    price=st.integers(min_value=1, max_value=100 * MICRO),
)
def test_billed_never_below_list(tokens: int, price: int) -> None:
    assert component_micro(tokens, price, 110) >= component_micro(tokens, price, 100)
