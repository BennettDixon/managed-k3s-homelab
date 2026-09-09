from gateway.auth import resolve_caller
from tests.conftest import TOKENS, load_test_registry


def test_resolves_valid_token_to_caller_with_class() -> None:
    registry = load_test_registry()
    caller = resolve_caller(f"Bearer {TOKENS['operator']}", TOKENS, registry)
    assert caller is not None
    assert caller.id == "operator"
    assert caller.class_ == "operator"
    executor = resolve_caller(f"Bearer {TOKENS['n8n-executor']}", TOKENS, registry)
    assert executor is not None and executor.class_ == "executor"


def test_rejects_missing_malformed_unknown() -> None:
    registry = load_test_registry()
    assert resolve_caller(None, TOKENS, registry) is None
    assert resolve_caller("", TOKENS, registry) is None
    assert resolve_caller(TOKENS["operator"], TOKENS, registry) is None
    assert resolve_caller("Basic abc", TOKENS, registry) is None
    assert resolve_caller("Bearer nope", TOKENS, registry) is None
    assert resolve_caller("Bearer " + TOKENS["operator"][:-1], TOKENS, registry) is None
    assert resolve_caller("bearer " + TOKENS["operator"], TOKENS, registry) is None


def test_token_without_registry_policy_authenticates_nobody() -> None:
    registry = load_test_registry()
    assert "ghost" not in registry.callers
    assert resolve_caller(f"Bearer {TOKENS['ghost']}", TOKENS, registry) is None
