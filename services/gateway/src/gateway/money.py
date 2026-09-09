"""Integer micro-USD arithmetic (spec §1 Units).

Money is stored as integer micro-USD; prices as integer micro-USD per MTok.
Every component is ``ceil(tokens × price / 1e6)``; sums and cap comparisons
are exact. Floats never touch a stored amount — they appear only when the
interface presents USD to a client.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MICRO = 1_000_000  # micro-USD per USD


class MoneyError(ValueError):
    """A USD value that cannot be represented exactly in micro-USD."""


def usd_to_micro(value: str | int | float | Decimal) -> int:
    """Exact conversion; more than six decimals or a non-finite value is an error.

    Floats are converted through ``repr`` (the shortest round-tripping decimal),
    which is what a YAML/JSON number such as ``6.25`` or ``0.1`` means to the
    human who typed it. Strings come from headers and env and are parsed as
    decimals — never via ``float``.
    """
    if isinstance(value, bool):
        raise MoneyError("boolean is not a USD amount")
    try:
        if isinstance(value, float):
            dec = Decimal(repr(value))
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, Decimal):
            dec = value
        else:
            dec = Decimal(value.strip())
    except (InvalidOperation, ValueError) as err:
        raise MoneyError(f"not a decimal USD amount: {value!r}") from err
    if not dec.is_finite():
        raise MoneyError("USD amount must be finite")
    scaled = dec * MICRO
    if scaled != scaled.to_integral_value():
        raise MoneyError("USD amount has more than six decimal places")
    return int(scaled)


def micro_to_usd_str(micro: int) -> str:
    """Six-decimal USD string for headers; exact."""
    dec = Decimal(micro) / MICRO
    return f"{dec:.6f}"


def micro_to_usd_float(micro: int) -> float:
    """USD as a JSON number for response bodies (display only; the ledger keeps integers)."""
    return float(Decimal(micro) / MICRO)


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise MoneyError("ceil_div by a non-positive denominator")
    return -(-numerator // denominator)


def component_micro(tokens: int, price_micro_per_mtok: int, multiplier_pct: int = 100) -> int:
    """``ceil(tokens × price × multiplier / 1e6)`` — one priced component.

    ``multiplier_pct`` is the billed-price multiplier (100 = list; 110 when the
    workspace is pinned to US inference, spec §5). It is applied before the
    ceiling so a billed component is never below its list component.
    """
    if tokens < 0 or price_micro_per_mtok < 0 or multiplier_pct <= 0:
        raise MoneyError("component_micro arguments must be non-negative (multiplier positive)")
    return ceil_div(tokens * price_micro_per_mtok * multiplier_pct, MICRO * 100)


def affordable_tokens(budget_micro: int, price_micro_per_mtok: int, multiplier_pct: int = 100) -> int:
    """Largest ``n`` with ``component_micro(n, price, multiplier) <= budget`` (0 when nothing fits)."""
    if budget_micro <= 0:
        return 0
    if price_micro_per_mtok <= 0 or multiplier_pct <= 0:
        raise MoneyError("affordable_tokens needs a positive price and multiplier")
    return (budget_micro * MICRO * 100) // (price_micro_per_mtok * multiplier_pct)


@dataclass(frozen=True)
class Prices:
    """Micro-USD per MTok for one model (registry §4)."""

    input: int
    output: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int


@dataclass(frozen=True)
class TokenUsage:
    """Provider-reported token counts (spec §5 Settle)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
        ):
            if getattr(self, name) < 0:
                raise MoneyError(f"{name} must be non-negative")


def usage_cost_micro(usage: TokenUsage, prices: Prices, multiplier_pct: int = 100) -> int:
    """Settle price for reported usage: every component ceil'd separately (spec §1)."""
    return (
        component_micro(usage.input_tokens, prices.input, multiplier_pct)
        + component_micro(usage.output_tokens, prices.output, multiplier_pct)
        + component_micro(usage.cache_write_5m_tokens, prices.cache_write_5m, multiplier_pct)
        + component_micro(usage.cache_write_1h_tokens, prices.cache_write_1h, multiplier_pct)
        + component_micro(usage.cache_read_tokens, prices.cache_read, multiplier_pct)
    )


def worst_case_micro(in_tokens: int, max_tokens: int, prices: Prices, multiplier_pct: int = 100) -> int:
    """Metered reservation (spec §5): ``in × input + max_tokens × output``.

    v1 sends no ``cache_control`` so no write rate enters, and thinking tokens
    bill as output inside ``max_tokens``.
    """
    return component_micro(in_tokens, prices.input, multiplier_pct) + component_micro(
        max_tokens, prices.output, multiplier_pct
    )


def reserve_input_tokens(counted: int) -> int:
    """``ceil(count_tokens × 1.05) + 32`` — the documented estimate plus margin (spec §5)."""
    if counted < 0:
        raise MoneyError("counted tokens must be non-negative")
    return ceil_div(counted * 105, 100) + 32


def heuristic_input_tokens(nbytes: int) -> int:
    """``ceil(bytes / 2.5)`` — the fallback when ``count_tokens`` is unavailable (spec §5)."""
    if nbytes < 0:
        raise MoneyError("byte count must be non-negative")
    return ceil_div(nbytes * 2, 5)
