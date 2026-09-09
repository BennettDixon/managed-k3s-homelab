import anthropic
import httpx2
import pytest

from gateway.upstream import (
    ANTHROPIC_BASE_URL,
    AnthropicMeteredClient,
    UpstreamFailure,
    UpstreamRequest,
    classify_failure,
    parse_retry_after,
)

REQUEST = httpx2.Request("POST", f"{ANTHROPIC_BASE_URL}/v1/messages")


def status_error(
    cls: type[anthropic.APIStatusError], status: int, *, headers: dict[str, str] | None = None, message: str = "boom"
) -> anthropic.APIStatusError:
    response = httpx2.Response(status, headers=headers or {}, request=REQUEST)
    return cls(message, response=response, body={"error": {"type": "x", "message": message}})


def test_rate_limit_with_retry_after_is_rate_limited_and_released() -> None:
    err = classify_failure(status_error(anthropic.RateLimitError, 429, headers={"retry-after": "3"}), started=False)
    assert err.kind == "rate_limited" and err.retry_after_s == 3.0 and err.proves_no_generation
    after = classify_failure(status_error(anthropic.RateLimitError, 429, headers={"retry-after": "3"}), started=True)
    assert after.kind == "rate_limited" and not after.proves_no_generation


def test_rate_limit_without_retry_after_is_the_spend_limit_shape() -> None:
    err = classify_failure(status_error(anthropic.RateLimitError, 429), started=False)
    assert err.kind == "spend_limit" and err.retry_after_s is None and err.proves_no_generation


@pytest.mark.parametrize("cls", [anthropic.AuthenticationError, anthropic.PermissionDeniedError])
def test_auth_errors(cls: type[anthropic.APIStatusError]) -> None:
    err = classify_failure(status_error(cls, cls.status_code), started=False)
    assert err.kind == "auth" and err.proves_no_generation


def test_bad_request_spend_limit_vs_rejected() -> None:
    limit = classify_failure(
        status_error(anthropic.BadRequestError, 400, message="Your credit balance is too low"), started=False
    )
    assert limit.kind == "spend_limit"
    rejected = classify_failure(
        status_error(anthropic.BadRequestError, 400, message="max_tokens: invalid"), started=False
    )
    assert rejected.kind == "rejected" and rejected.proves_no_generation


@pytest.mark.parametrize(("cls", "status"), [(anthropic.InternalServerError, 500), (anthropic.OverloadedError, 529)])
def test_server_errors_release_only_before_generation(cls: type[anthropic.APIStatusError], status: int) -> None:
    before = classify_failure(status_error(cls, status), started=False)
    assert before.kind == "server" and before.status == status and before.proves_no_generation
    after = classify_failure(status_error(cls, status), started=True)
    assert after.kind == "server" and not after.proves_no_generation


def test_timeout_never_proves_no_generation() -> None:
    err = classify_failure(anthropic.APITimeoutError(request=REQUEST), started=False)
    assert err.kind == "timeout" and not err.proves_no_generation


def test_connection_refused_before_body_vs_severed_read() -> None:
    refused = anthropic.APIConnectionError(request=REQUEST)
    refused.__cause__ = httpx2.ConnectError("connection refused")
    err = classify_failure(refused, started=False)
    assert err.kind == "network" and err.before_generation and err.proves_no_generation

    severed = anthropic.APIConnectionError(request=REQUEST)
    severed.__cause__ = httpx2.ReadError("reset by peer")
    err = classify_failure(severed, started=False)
    assert err.kind == "network" and not err.before_generation and not err.proves_no_generation

    err = classify_failure(refused, started=True)
    assert not err.proves_no_generation


def test_garbage_is_never_released() -> None:
    assert classify_failure(ValueError("no json"), started=False).kind == "bad_response"
    assert not classify_failure(KeyError("usage"), started=False).proves_no_generation
    assert classify_failure(RuntimeError("weird"), started=True).kind == "bad_response"


def test_parse_retry_after_forms() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now_s=1445412000.0) == 480.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now_s=1_900_000_000.0) == 0.0


def test_upstream_request_shapes() -> None:
    req = UpstreamRequest(
        model="claude-opus-5",
        system="sys",
        messages=(("user", "héllo"), ("assistant", "ok")),
        max_tokens=10,
        effort="low",
        json_schema={"type": "object"},
    )
    assert req.text_bytes() == len(b"sys") + len("héllo".encode()) + len(b"ok")
    assert req.anthropic_messages() == [{"role": "user", "content": "héllo"}, {"role": "assistant", "content": "ok"}]
    assert req.output_config() == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert UpstreamRequest(model="m", system=None, messages=(("user", "x"),), max_tokens=1).output_config() is None


def test_real_client_is_pinned_to_the_hardcoded_base_url_with_no_retries() -> None:
    client = AnthropicMeteredClient("sk-ant-test")
    inner = client._client
    assert str(inner.base_url).rstrip("/") == ANTHROPIC_BASE_URL
    assert inner.max_retries == 0
    assert inner.api_key == "sk-ant-test"
    assert inner.auth_token is None


def test_failure_message_and_flags() -> None:
    failure = UpstreamFailure("auth", status=401, before_generation=True, message="bad key")
    assert "auth" in str(failure) and failure.proves_no_generation
