import json
from pathlib import Path

import pytest

from gateway.config import ConfigError, load_config, parse_caller_tokens
from tests.conftest import base_env


def test_parse_caller_tokens_rejects_non_json_arrays_short_tokens_bad_ids_empty() -> None:
    with pytest.raises(ConfigError, match="valid JSON"):
        parse_caller_tokens("not json")
    with pytest.raises(ConfigError, match="object"):
        parse_caller_tokens('["a"]')
    with pytest.raises(ConfigError, match="16 chars") as short:
        parse_caller_tokens('{"swapped-token-value": "short"}')
    assert "swapped-token-value" not in str(short.value)  # a key/value swap must not echo the key
    with pytest.raises(ConfigError, match="identifier"):
        parse_caller_tokens('{"Bad Id!": "0123456789abcdef"}')
    with pytest.raises(ConfigError, match="no callers"):
        parse_caller_tokens("{}")


def test_parse_caller_tokens_never_echoes_the_offending_key() -> None:
    with pytest.raises(ConfigError) as info:
        parse_caller_tokens(json.dumps({"sk-ant-SECRET-VALUE-0123456789": "operator"}))
    assert "SECRET" not in str(info.value)
    assert "position 1" in str(info.value)


def test_parse_caller_tokens_rejects_duplicate_values() -> None:
    with pytest.raises(ConfigError, match="duplicates"):
        parse_caller_tokens(json.dumps({"caller-one": "same-token-0123456789", "caller-two": "same-token-0123456789"}))


def test_load_config_defaults(tmp_path: Path) -> None:
    config = load_config(base_env(tmp_path))
    assert config.port == 8080
    assert config.max_request_cap_micro == 25_000_000
    assert config.metered_daily_ceiling_micro == 5_000_000
    assert config.subscription_daily_list_ceiling_micro == 10_000_000
    assert config.provider_timeout_ms == 300_000
    assert config.lane_probe_interval_ms == 0
    assert config.billed_price_multiplier_pct == 100
    assert config.body_limit_bytes == 1_048_576
    assert set(config.caller_tokens) >= {"operator", "n8n-executor"}


@pytest.mark.parametrize(
    ("name", "value", "pattern"),
    [
        ("PORT", "", "set but empty"),
        ("PORT", "abc", "integer"),
        ("PORT", "70000", "integer"),
        ("MAX_REQUEST_CAP_USD", "25 USD", "decimal"),
        ("MAX_REQUEST_CAP_USD", "0.0000001", "decimal"),
        ("MAX_REQUEST_CAP_USD", "0", "USD amount in"),
        ("GATEWAY_METERED_DAILY_CEILING_USD", "NaN", "finite"),
        ("PROVIDER_TIMEOUT_MS", "999999", "integer"),
        ("BILLED_PRICE_MULTIPLIER_PCT", "99", "integer"),
        ("LANE_PROBE_INTERVAL_MS", "-1", "integer"),
        ("DB_PATH", "", "set but empty"),
        ("REGISTRY_PATH", "", "set but empty"),
    ],
)
def test_load_config_is_loud(tmp_path: Path, name: str, value: str, pattern: str) -> None:
    with pytest.raises(ConfigError, match=pattern):
        load_config(base_env(tmp_path, **{name: value}))


@pytest.mark.parametrize("missing", ["GATEWAY_CALLER_TOKENS", "ANTHROPIC_API_KEY"])
def test_load_config_requires_secrets(tmp_path: Path, missing: str) -> None:
    env = base_env(tmp_path)
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


@pytest.mark.parametrize(
    "ambient", ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_PROFILE"]
)
def test_load_config_refuses_ambient_sdk_overrides(tmp_path: Path, ambient: str) -> None:
    with pytest.raises(ConfigError, match="ambiently"):
        load_config(base_env(tmp_path, **{ambient: "x"}))


def test_load_config_parses_usd_and_multiplier(tmp_path: Path) -> None:
    config = load_config(
        base_env(
            tmp_path,
            GATEWAY_METERED_DAILY_CEILING_USD="2.50",
            BILLED_PRICE_MULTIPLIER_PCT="110",
            MAX_REQUEST_CAP_USD="10",
        )
    )
    assert config.metered_daily_ceiling_micro == 2_500_000
    assert config.billed_price_multiplier_pct == 110
    assert config.max_request_cap_micro == 10_000_000
