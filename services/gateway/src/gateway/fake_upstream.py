"""A scriptable metered client for tests and the local smoke — spends nothing, reaches nothing."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from gateway.money import TokenUsage
from gateway.upstream import FailureKind, OnStarted, UpstreamFailure, UpstreamRequest, UpstreamResult


@dataclass(frozen=True)
class FakeReply:
    text: str = "ok"
    input_tokens: int = 10
    output_tokens: int = 5
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    model: str | None = None
    request_id: str = "req_fake"
    stop_reason: str = "end_turn"
    inference_geo: str | None = None
    delay_s: float = 0.0  # after message_start, before the reply (disconnect / cancellation tests)


@dataclass(frozen=True)
class FakeFailure:
    kind: FailureKind
    status: int | None = None
    retry_after_s: float | None = None
    started_before_failure: bool = False
    # None: derived from started_before_failure. False without a start models a
    # severed body (the real client proves "no generation" only for a refused
    # connection); explicit so tests choose the outcome the spec names.
    before_generation: bool | None = None
    message: str = "fake failure"


@dataclass(frozen=True)
class FakeHang:
    """Blocks until cancelled; optionally after emitting message_start."""

    started: bool = True


Outcome = FakeReply | FakeFailure | FakeHang


@dataclass
class FakeMeteredClient:
    script: Sequence[Outcome] = ()
    default: Outcome = field(default_factory=FakeReply)
    count_tokens_failure: UpstreamFailure | None = None
    probe_failure: UpstreamFailure | None = None
    attempts: int = 0
    generations: int = 0  # calls that emitted message_start
    count_tokens_calls: int = 0
    probe_calls: int = 0
    requests_seen: list[UpstreamRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue: deque[Outcome] = deque(self.script)

    def push(self, *outcomes: Outcome) -> None:
        self._queue.extend(outcomes)

    async def count_tokens(self, request: UpstreamRequest) -> int:
        self.count_tokens_calls += 1
        if self.count_tokens_failure is not None:
            raise self.count_tokens_failure
        return max(1, request.text_bytes() // 4)

    async def complete(self, request: UpstreamRequest, *, on_started: OnStarted, timeout_s: float) -> UpstreamResult:
        self.attempts += 1
        self.requests_seen.append(request)
        outcome = self._queue.popleft() if self._queue else self.default
        if isinstance(outcome, FakeHang):
            if outcome.started:
                self.generations += 1
                await on_started()
            await asyncio.Event().wait()  # until cancelled
            raise AssertionError("unreachable")
        if isinstance(outcome, FakeFailure):
            if outcome.started_before_failure:
                self.generations += 1
                await on_started()
            before = outcome.before_generation
            if before is None:
                before = not outcome.started_before_failure
            raise UpstreamFailure(
                outcome.kind,
                status=outcome.status,
                retry_after_s=outcome.retry_after_s,
                before_generation=before,
                message=outcome.message,
            )
        self.generations += 1
        await on_started()
        if outcome.delay_s:
            await asyncio.sleep(outcome.delay_s)
        return UpstreamResult(
            text=outcome.text,
            stop_reason=outcome.stop_reason,
            model_used=outcome.model or request.model,
            usage=TokenUsage(
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                cache_write_5m_tokens=outcome.cache_write_5m_tokens,
                cache_write_1h_tokens=outcome.cache_write_1h_tokens,
                cache_read_tokens=outcome.cache_read_tokens,
            ),
            inference_geo=outcome.inference_geo,
            provider_request_id=outcome.request_id,
        )

    async def probe(self) -> None:
        self.probe_calls += 1
        if self.probe_failure is not None:
            raise self.probe_failure
