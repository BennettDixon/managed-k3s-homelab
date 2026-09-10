from datetime import date

import pytest
import yaml

from gateway.registry import RegistryError, check_workspace_limit, parse_registry, resolve_model
from tests.conftest import REGISTRY_YAML


def mutate(**changes: object) -> str:
    """Apply dotted-path edits to the test registry and return YAML."""
    doc = yaml.safe_load(REGISTRY_YAML)
    for path, value in changes.items():
        parts = path.split("__")
        node = doc
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    return yaml.safe_dump(doc)


def test_parses_and_converts_prices_to_micro() -> None:
    registry = parse_registry(REGISTRY_YAML)
    opus = registry.models["claude-opus-5"]
    assert opus.prices.input == 5_000_000
    assert opus.prices.cache_read == 500_000
    assert opus.prices.cache_write_5m == 6_250_000
    assert registry.projects["homelab-ops"].cap_micro == 20_000_000
    assert registry.callers["operator"].max_request_cap_micro == 5_000_000
    assert registry.callers["operator"].max_day_billed_micro == 5_000_000
    assert registry.callers["worker-01"].class_ == "lane-agent"
    assert registry.callers["worker-01"].lane == "subscription"
    assert resolve_model(registry, "haiku") is registry.models["claude-haiku-4-5"]
    assert resolve_model(registry, "claude-haiku-4-5") is registry.models["claude-haiku-4-5"]
    assert resolve_model(registry, "gpt-4") is None
    assert len(registry.prices_hash) == 64
    assert registry.prices_as_of == date(2026, 9, 1)


def test_prices_hash_tracks_prices_only() -> None:
    a = parse_registry(REGISTRY_YAML).prices_hash
    b = parse_registry(mutate(**{"projects__homelab-ops__cap_usd": 30})).prices_hash
    c = parse_registry(mutate(**{"models__claude-opus-5__input": 5.5})).prices_hash
    assert a == b
    assert a != c


@pytest.mark.parametrize(
    ("changes", "pattern"),
    [
        ({"models__claude-opus-5__input": 0.4}, "cache_read <= input"),  # transposed columns
        ({"models__claude-opus-5__cache_write_1h": 6.0}, "cache_write_5m <= cache_write_1h"),
        ({"models__claude-opus-5__input": 0}, "must be > 0"),
        ({"models__claude-opus-5__input": 5.0000001}, "micro-USD"),
        ({"models__claude-opus-5__default_max_tokens": 32000}, "default_max_tokens exceeds"),
        ({"models__claude-opus-5__cli_alias": None}, "cli_alias"),
        ({"prices_as_of": "2999-01-01"}, "in the future"),
        ({"projects__homelab-ops__default_lane": "subscription"}, "default_lane"),
        ({"projects__homelab-ops__lanes": ["subscription", "metered"]}, "does not serve lane"),  # haiku is metered-only
        ({"projects__homelab-ops__cap_usd": 20000}, "cap_usd must be in"),
        ({"projects__homelab-ops__cap_usd": -1}, "cap_usd must be in"),
        ({"projects__homelab-ops__max_request_cap_usd": 25}, "max_request_cap_usd must be in"),
        ({"projects__homelab-ops__default_model": "claude-opus-5-x"}, "default_model"),
        ({"projects__homelab-ops__models": ["claude-sonnet-5", "nope"]}, "unknown model"),
        ({"projects__homelab-ops__period": "day"}, "period"),
        (
            {"projects__sub-only__lanes": ["subscription"], "projects__sub-only__fallback_allowed": True},
            "fallback_allowed",
        ),
        ({"callers__operator__max_request_cap_usd": 30}, "MAX_REQUEST_CAP_USD"),
        ({"callers__operator__max_request_cap_usd": None}, "max_request_cap_usd is required"),
        ({"callers__operator__projects": []}, "non-empty projects"),
        ({"callers__operator__projects": ["nope"]}, "unknown project"),
        ({"callers__operator__lanes": ["metered", "subscription"]}, "not granted by project"),
        ({"callers__operator__lanes": None}, "lanes must be declared"),
        (
            {"callers__n8n-executor__projects": ["sub-only"], "callers__n8n-executor__lanes": ["subscription"]},
            "operator-only",
        ),
        ({"callers__worker-01__projects": ["homelab-ops"]}, "carries only"),
        ({"callers__worker-01__lane": None}, "requires `lane`"),
        ({"callers__operator__lane": "metered"}, "only for class lane-agent"),
        ({"callers__operator__class": "frontend"}, "schema"),  # the enum is closed
        ({"callers__nanoclaw": {"class": "frontend"}}, "schema"),
        ({"aliases__haiku": "nope"}, "unknown model"),
        ({"aliases__claude-opus-5": "claude-opus-5"}, "collides"),
        (
            {
                "models__Bad Model": {
                    "input": 1,
                    "output": 1,
                    "cache_write_5m": 1,
                    "cache_write_1h": 1,
                    "cache_read": 1,
                    "max_tokens": 1,
                    "default_max_tokens": 1,
                    "effort": False,
                    "lanes": ["metered"],
                }
            },
            "must match",
        ),
        ({"callers__operator__max_day_billed_usd": 0}, "max_day_billed_usd"),
        ({"console_workspace_limit_usd": 0}, "console_workspace_limit_usd"),
        ({"models__claude-opus-5__extra": 1}, "schema"),
        ({"projects__homelab-ops__cap_usd": "20"}, "schema"),  # strict: numbers only
    ],
)
def test_admission_rules(changes: dict[str, object], pattern: str) -> None:
    with pytest.raises(RegistryError, match=pattern):
        parse_registry(mutate(**changes))


def test_not_a_mapping_and_bad_yaml() -> None:
    with pytest.raises(RegistryError, match="mapping"):
        parse_registry("- a\n- b\n")
    with pytest.raises(RegistryError, match="YAML"):
        parse_registry("a: [\n")


def test_max_request_cap_env_bounds_callers() -> None:
    with pytest.raises(RegistryError, match="MAX_REQUEST_CAP_USD"):
        parse_registry(REGISTRY_YAML, max_request_cap_micro=1_000_000)


def test_workspace_limit_is_a_ci_check_not_admission() -> None:
    text = mutate(console_workspace_limit_usd=10)
    registry = parse_registry(text)  # parses fine
    with pytest.raises(RegistryError, match="exceeds console_workspace_limit_usd"):
        check_workspace_limit(registry)
    check_workspace_limit(parse_registry(REGISTRY_YAML))


def test_today_parameter_controls_future_check() -> None:
    with pytest.raises(RegistryError, match="future"):
        parse_registry(REGISTRY_YAML, today=date(2026, 8, 31))
    parse_registry(REGISTRY_YAML, today=date(2026, 9, 1))
