"""Environment configuration, validated loudly at boot (spec §8; jobs-mcp config.ts idiom).

A bare numeric coercion turns a templating slip ("" or "25 USD") into a value
that silently disables a guard; every number here is parsed strictly, with
its bounds, and an empty-but-set variable is an error rather than a default.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from gateway.ids import ID_RE
from gateway.money import MoneyError, usd_to_micro

# Ambient credential / endpoint resolution the Anthropic SDK performs from the
# environment. The gateway passes its key and base URL explicitly (spec §6.1:
# never ambient resolution; one hard-coded base URL as the in-code egress
# fence) and refuses to boot if any of these could redirect or re-credential
# the client behind its back.
FORBIDDEN_AMBIENT_ENV: tuple[str, ...] = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE",
)

BODY_LIMIT_BYTES = 1_048_576  # spec §2: 1 MiB


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    port: int
    db_path: str
    registry_path: str
    caller_tokens: Mapping[str, str]
    anthropic_api_key: str
    max_request_cap_micro: int
    metered_daily_ceiling_micro: int
    subscription_daily_list_ceiling_micro: int
    provider_timeout_ms: int
    lane_probe_interval_ms: int
    lane_probe_down_interval_ms: int
    billed_price_multiplier_pct: int
    body_limit_bytes: int = BODY_LIMIT_BYTES


def _raw(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    if raw == "":
        raise ConfigError(f"env {name} is set but empty")
    return raw


def _int_env(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as err:
        raise ConfigError(f"env {name}={raw!r} must be an integer in [{lo}, {hi}]") from err
    if not lo <= value <= hi:
        raise ConfigError(f"env {name}={raw!r} must be an integer in [{lo}, {hi}]")
    return value


def _usd_env(env: Mapping[str, str], name: str, default_micro: int, lo_micro: int, hi_micro: int) -> int:
    raw = _raw(env, name)
    if raw is None:
        return default_micro
    try:
        value = usd_to_micro(raw)
    except MoneyError as err:
        raise ConfigError(f"env {name}={raw!r}: {err}") from err
    if not lo_micro <= value <= hi_micro:
        raise ConfigError(f"env {name}={raw!r} must be a USD amount in [{lo_micro / 1e6}, {hi_micro / 1e6}]")
    return value


def parse_caller_tokens(raw: str) -> dict[str, str]:
    """Port of knowledge-mcp's parseCallerTokens (spec §2).

    A truncated or single-string secret must fail the pod, not silently
    authorize nobody. The offending key is deliberately NOT echoed: a swapped
    key/value pair would otherwise print a token into the shipped boot log.
    Duplicate token values are refused because the constant-time resolver
    iterates every candidate and would resolve to whichever id sorts last —
    silent identity confusion from a paste error.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as err:
        raise ConfigError("GATEWAY_CALLER_TOKENS is not valid JSON") from err
    if not isinstance(parsed, dict):
        raise ConfigError("GATEWAY_CALLER_TOKENS must be a JSON object of caller_id -> token")
    tokens: dict[str, str] = {}
    seen: set[str] = set()
    for position, (caller_id, token) in enumerate(parsed.items(), start=1):
        if not isinstance(caller_id, str) or not ID_RE.match(caller_id):
            raise ConfigError(
                f"caller id at map position {position} is not a valid identifier (must match {ID_RE.pattern})"
            )
        if not isinstance(token, str) or len(token) < 16:
            raise ConfigError(f"caller {caller_id}: token must be a string of at least 16 chars")
        if token in seen:
            raise ConfigError(f"caller {caller_id}: token value duplicates another caller's")
        seen.add(token)
        tokens[caller_id] = token
    if not tokens:
        raise ConfigError("GATEWAY_CALLER_TOKENS contains no callers")
    return tokens


def load_config(env: Mapping[str, str]) -> Config:
    for name in FORBIDDEN_AMBIENT_ENV:
        if name in env:
            raise ConfigError(
                f"env {name} is set: the gateway never resolves credentials or endpoints ambiently (spec §6.1)"
            )
    tokens_raw = _raw(env, "GATEWAY_CALLER_TOKENS")
    if tokens_raw is None:
        raise ConfigError("missing required env: GATEWAY_CALLER_TOKENS")
    api_key = _raw(env, "ANTHROPIC_API_KEY")
    if api_key is None:
        raise ConfigError("missing required env: ANTHROPIC_API_KEY")
    max_request_cap = _usd_env(env, "MAX_REQUEST_CAP_USD", 25_000_000, 10_000, 10_000_000_000)
    return Config(
        port=_int_env(env, "PORT", 8080, 1, 65535),
        db_path=env.get("DB_PATH") or "/data/gateway.db",
        registry_path=env.get("REGISTRY_PATH") or "/config/registry.yaml",
        caller_tokens=parse_caller_tokens(tokens_raw),
        anthropic_api_key=api_key,
        max_request_cap_micro=max_request_cap,
        # The two env-level daily brakes (spec §5): gateway-wide, one number each.
        metered_daily_ceiling_micro=_usd_env(
            env, "GATEWAY_METERED_DAILY_CEILING_USD", 5_000_000, 10_000, 10_000_000_000
        ),
        # Inert until the deferred subscription lane ships; validated so a
        # typo is caught now rather than on the day the lane lands.
        subscription_daily_list_ceiling_micro=_usd_env(
            env, "GATEWAY_SUBSCRIPTION_DAILY_LIST_CEILING_USD", 10_000_000, 10_000, 10_000_000_000
        ),
        provider_timeout_ms=_int_env(env, "PROVIDER_TIMEOUT_MS", 300_000, 1_000, 600_000),
        # Idle probe cadence (spec §6.1): models.list() every 5 min while up,
        # every 60 s while down. 0 disables the probe.
        lane_probe_interval_ms=_int_env(env, "LANE_PROBE_INTERVAL_MS", 300_000, 0, 3_600_000),
        lane_probe_down_interval_ms=_int_env(env, "LANE_PROBE_DOWN_INTERVAL_MS", 60_000, 1_000, 3_600_000),
        # 110 when the Console workspace is pinned to US inference (spec §5,
        # verify usage.inference_geo at slice 2); applied to reservation AND
        # settle so the billed invariant settled <= reserved is unaffected.
        billed_price_multiplier_pct=_int_env(env, "BILLED_PRICE_MULTIPLIER_PCT", 100, 100, 200),
    )
