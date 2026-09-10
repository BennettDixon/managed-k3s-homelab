"""The metered lane's provider client (spec §6.1) behind an interface with a fake.

Official Anthropic SDK against the native Messages API — never the vendor's
OpenAI-compatibility endpoint (it hides the cache-token split the ledger
prices). ``max_retries=0``: one provider call per ledger row. Always streams
upstream so no headers timeout severs a call mid-spend. Key passed
explicitly, one hard-coded base URL, ``trust_env=False`` (the in-code egress
fence: no proxy or CA override from the pod environment).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol, cast

import anthropic
import httpx2
from anthropic.types import MessageParam

from gateway.money import TokenUsage

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
# Connect budget for every provider call: a route that drops packets must fail
# like a refused connection (released, counted toward lane-down) instead of
# burning the whole read timeout and then charging the reservation.
CONNECT_TIMEOUT_S = 10.0
COUNT_TOKENS_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 15.0

FailureKind = Literal[
    "rate_limited",  # 429 with retry-after: released, lane stays up
    "spend_limit",  # org/workspace limit: released, lane down until resume
    "auth",  # 401/403: released, lane auth failed
    "rejected",  # other 4xx before generation: released
    "server",  # 5xx/529 (or an in-stream overloaded/api error): released when before message_start
    "network",  # connection refused / connect timeout (released) or a severed body (never released)
    "timeout",  # read timeout: never released
    "bad_response",  # 2xx with garbage: never released
]


@dataclass(frozen=True)
class UpstreamRequest:
    model: str
    system: str | None
    messages: tuple[tuple[str, str], ...]  # (role, text)
    max_tokens: int
    stop_sequences: tuple[str, ...] = ()
    effort: str | None = None
    json_schema: dict[str, object] | None = None

    def text_bytes(self) -> int:
        """UTF-8 bytes of everything the provider reads as input — the heuristic's base.

        A ``json_schema`` is billed input (grammar tokens), so it counts; stop
        sequences are tiny but counted for the same reason.
        """
        total = len(self.system.encode("utf-8")) if self.system else 0
        total += sum(len(text.encode("utf-8")) for _, text in self.messages)
        if self.json_schema is not None:
            total += len(json.dumps(self.json_schema, separators=(",", ":")).encode("utf-8"))
        total += sum(len(stop.encode("utf-8")) for stop in self.stop_sequences)
        return total

    def anthropic_messages(self) -> list[MessageParam]:
        return [cast(MessageParam, {"role": role, "content": text}) for role, text in self.messages]

    def output_config(self) -> dict[str, object] | None:
        config: dict[str, object] = {}
        if self.effort is not None:
            config["effort"] = self.effort
        if self.json_schema is not None:
            config["format"] = {"type": "json_schema", "schema": self.json_schema}
        return config or None


@dataclass(frozen=True)
class UpstreamResult:
    text: str
    stop_reason: str | None
    model_used: str
    usage: TokenUsage
    inference_geo: str | None
    provider_request_id: str | None
    http_status: int = 200


class UpstreamFailure(Exception):
    def __init__(
        self,
        kind: FailureKind,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        before_generation: bool,
        message: str = "",
        provider_request_id: str | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.status = status
        self.retry_after_s = retry_after_s
        self.before_generation = before_generation
        self.message = message
        self.provider_request_id = provider_request_id
        self.error_type = error_type

    @property
    def proves_no_generation(self) -> bool:
        """Release is allowed only when the outcome proves no generation (spec §1)."""
        return self.before_generation and self.kind in {
            "rate_limited",
            "spend_limit",
            "auth",
            "rejected",
            "server",
            "network",
        }


OnStarted = Callable[[], Awaitable[None]]


class MeteredClient(Protocol):
    async def count_tokens(self, request: UpstreamRequest) -> int: ...

    async def complete(
        self, request: UpstreamRequest, *, on_started: OnStarted, timeout_s: float
    ) -> UpstreamResult: ...

    async def probe(self) -> None: ...


def parse_retry_after(value: str | None, now_s: float | None = None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, stamp.timestamp() - (now_s if now_s is not None else time.time()))
    return max(0.0, seconds)


def _usage_from(usage: anthropic.types.Usage) -> TokenUsage:
    creation = usage.cache_creation
    if creation is not None:
        write_5m = creation.ephemeral_5m_input_tokens
        write_1h = creation.ephemeral_1h_input_tokens
    else:
        write_5m = usage.cache_creation_input_tokens or 0
        write_1h = 0
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
    )


_SPEND_LIMIT_MARKERS = ("spend limit", "spending limit", "credit balance", "billing", "usage limit")


def _kind_from_error_type(error_type: str | None) -> FailureKind | None:
    """The provider's error `type` — the only signal an in-stream error event carries."""
    if error_type in ("overloaded_error", "api_error"):
        return "server"
    if error_type == "rate_limit_error":
        return "rate_limited"
    if error_type in ("authentication_error", "permission_error"):
        return "auth"
    if error_type == "billing_error":
        return "spend_limit"
    if error_type == "invalid_request_error":
        return "rejected"
    return None


def _classify_status(err: anthropic.APIStatusError, *, started: bool) -> UpstreamFailure:
    status = err.status_code
    retry_after = parse_retry_after(err.response.headers.get("retry-after"))
    error_type = err.type

    def make(kind: FailureKind) -> UpstreamFailure:
        return UpstreamFailure(
            kind,
            status=status,
            retry_after_s=retry_after,
            before_generation=not started,
            message=err.message,
            provider_request_id=err.request_id,
            error_type=error_type,
        )

    if error_type == "billing_error":
        # A 403 billing_error is the workspace's credit/spend limit, never the key:
        # marking the lane auth-failed would flap it against a passing probe.
        return make("spend_limit")
    if status == 429:
        # A 429 WITHOUT retry-after is the org/workspace spend-limit shape (verify at slice 2).
        return make("rate_limited" if retry_after is not None else "spend_limit")
    if status in (401, 403):
        return make("auth")
    if status == 400:
        lowered = err.message.lower()
        return make("spend_limit" if any(marker in lowered for marker in _SPEND_LIMIT_MARKERS) else "rejected")
    if status >= 500:
        return make("server")
    if status == 200:
        # An `error` event inside a 200 SSE stream: the SDK raises it with the
        # stream's own status, so only the error type says what happened.
        return make(_kind_from_error_type(error_type) or "rejected")
    return make("rejected")


def classify_failure(err: Exception, *, started: bool) -> UpstreamFailure:
    """Map an SDK exception onto the spec §6.1 error mapping."""
    if isinstance(err, anthropic.APIStatusError):
        return _classify_status(err, started=started)
    if isinstance(err, anthropic.APITimeoutError):
        cause = err.__cause__
        if isinstance(cause, httpx2.ConnectTimeout | httpx2.PoolTimeout):
            # No connection was made, so nothing could have generated: the refused-connection bucket.
            return UpstreamFailure("network", before_generation=not started, message=f"connect timeout: {cause!r}")
        return UpstreamFailure("timeout", before_generation=not started, message="provider read timeout")
    if isinstance(err, anthropic.APIConnectionError):
        # Only a connection error before the request body was sent proves no generation.
        refused = isinstance(err.__cause__, httpx2.ConnectError)
        return UpstreamFailure("network", before_generation=(not started) and refused, message=str(err))
    if isinstance(err, anthropic.APIResponseValidationError | ValueError | KeyError | TypeError):
        return UpstreamFailure("bad_response", before_generation=False, message=str(err))
    return UpstreamFailure("bad_response", before_generation=False, message=f"{type(err).__name__}: {err}")


def call_timeout(total_s: float) -> httpx2.Timeout:
    """A scalar timeout would make the CONNECT phase as long as the read phase (spec §6.1)."""
    return httpx2.Timeout(total_s, connect=min(CONNECT_TIMEOUT_S, total_s))


class AnthropicMeteredClient:
    def __init__(self, api_key: str) -> None:
        # Explicit key and base URL: the SDK then reads no credential env var
        # (verified against anthropic 1.4.0's constructor) and config.py refuses
        # to boot with the ambient overrides set at all. trust_env=False keeps
        # HTTPS_PROXY / SSL_CERT_FILE in the pod env from rerouting or
        # re-trusting the dollar-credential egress.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=ANTHROPIC_BASE_URL,
            max_retries=0,
            timeout=call_timeout(60.0),
            http_client=anthropic.DefaultAsyncHttpxClient(trust_env=False),
        )

    async def count_tokens(self, request: UpstreamRequest) -> int:
        params: dict[str, Any] = {"model": request.model, "messages": request.anthropic_messages()}
        if request.system:
            params["system"] = request.system
        output_config = request.output_config()
        if output_config is not None:
            params["output_config"] = output_config
        try:
            result = await self._client.messages.count_tokens(**params, timeout=call_timeout(COUNT_TOKENS_TIMEOUT_S))
        except Exception as err:
            raise classify_failure(err, started=False) from err
        return int(result.input_tokens)

    async def complete(self, request: UpstreamRequest, *, on_started: OnStarted, timeout_s: float) -> UpstreamResult:
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": request.anthropic_messages(),
            "service_tier": "standard_only",
        }
        if request.system:
            params["system"] = request.system
        if request.stop_sequences:
            params["stop_sequences"] = list(request.stop_sequences)
        output_config = request.output_config()
        if output_config is not None:
            params["output_config"] = output_config
        started = False
        try:
            async with self._client.messages.stream(**params, timeout=call_timeout(timeout_s)) as stream:
                async for event in stream:
                    if event.type == "message_start" and not started:
                        started = True
                        await on_started()
                final = await stream.get_final_message()
                request_id = stream.request_id
        except Exception as err:
            raise classify_failure(err, started=started) from err
        if not started:
            raise UpstreamFailure("bad_response", before_generation=False, message="stream ended without message_start")
        text = "".join(block.text for block in final.content if block.type == "text")
        return UpstreamResult(
            text=text,
            stop_reason=final.stop_reason,
            model_used=str(final.model),
            usage=_usage_from(final.usage),
            inference_geo=final.usage.inference_geo,
            provider_request_id=request_id,
        )

    async def probe(self) -> None:
        """``models.list()``: authenticated, unbilled, no token-counting bucket (spec §6.1)."""
        try:
            await self._client.models.list(limit=1, timeout=call_timeout(PROBE_TIMEOUT_S))
        except Exception as err:
            raise classify_failure(err, started=False) from err
