"""The accepted OpenAI subset (spec §3) and its translation to the Messages API.

Errors over surprises: unsupported request fields are rejected by name, never
silently dropped. The four sampling parameters are the one exception — they
are stripped and declared in ``X-Gateway-Ignored`` because every stock client
sends a temperature and current models reject one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.errors import GatewayError, forbidden, schema, unsupported
from gateway.money import TokenUsage
from gateway.registry import Model, Project, Registry, resolve_model
from gateway.upstream import UpstreamRequest

ACCEPTED_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "response_format",
        "reasoning_effort",
        "stream",
    }
)
# Ignored and never recorded (spec §3 / §9.3).
IGNORED_NEVER_RECORDED: frozenset[str] = frozenset({"user", "metadata", "store"})
# Stripped with X-Gateway-Ignored (spec §3).
# ... plus stream_options: usage always rides the final chunk of the buffered stream.
STRIPPED_SAMPLING: tuple[str, ...] = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "stream_options")
# The keys an OpenAI SDK serialises on the assistant message it hands back
# (spec §2 "zero client changes"): accepted only when null or empty.
ASSISTANT_ROUND_TRIP: frozenset[str] = frozenset({"refusal", "tool_calls", "function_call", "annotations", "audio"})
JSON_OBJECT_INSTRUCTION = "Respond with a single valid JSON object and nothing else."
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
MAX_STOP_SEQUENCES = 4


class _JsonSchemaSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    name: str
    schema_: dict[str, object] = Field(alias="schema")
    strict: bool | None = None
    description: str | None = None


class _ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["text", "json_object", "json_schema"]
    json_schema: _JsonSchemaSpec | None = None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str | None = None
    messages: list[object] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    response_format: _ResponseFormat | None = None
    reasoning_effort: EffortLevel | None = None
    stream: bool = False


@dataclass(frozen=True)
class ChatMessage:
    role: str
    text: str


@dataclass(frozen=True)
class ParsedChat:
    model: str | None
    messages: tuple[ChatMessage, ...]
    max_tokens: int | None
    stop: tuple[str, ...]
    response_format: str
    json_schema: dict[str, object] | None
    reasoning_effort: str | None
    stream: bool
    ignored: tuple[str, ...]


def _first_error(err: ValidationError) -> tuple[str, str]:
    first = err.errors()[0]
    loc = ".".join(str(part) for part in first["loc"])
    return loc, str(first["msg"])


def _parse_messages(raw_messages: list[object]) -> tuple[ChatMessage, ...]:
    out: list[ChatMessage] = []
    for index, item in enumerate(raw_messages):
        where = f"messages[{index}]"
        if not isinstance(item, dict):
            raise schema(f"{where} must be an object", param=where)
        role = item.get("role")
        for key, value in item.items():
            if key in ("role", "content"):
                continue
            if role == "assistant" and key in ASSISTANT_ROUND_TRIP and value in (None, [], {}):
                continue  # our own reply echoed back by a stock client
            raise unsupported(f"{where}.{key} is not supported", param=f"{where}.{key}")
        if role in ("tool", "function"):
            raise unsupported(f"{where}.role {role!r}: tools are not supported", param=f"{where}.role")
        if role not in ("system", "developer", "user", "assistant"):
            raise schema(f"{where}.role must be system, developer, user or assistant", param=f"{where}.role")
        content = item.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for part_index, part in enumerate(content):
                part_where = f"{where}.content[{part_index}]"
                if not isinstance(part, dict):
                    raise schema(f"{part_where} must be an object", param=part_where)
                part_type = part.get("type")
                if part_type != "text":
                    raise unsupported(
                        f"{part_where}.type {part_type!r} is not supported (text only)",
                        param=f"{part_where}.type",
                    )
                for key in part:
                    if key not in ("type", "text"):
                        raise unsupported(f"{part_where}.{key} is not supported", param=f"{part_where}.{key}")
                text_value = part.get("text")
                if not isinstance(text_value, str):
                    raise schema(f"{part_where}.text must be a string", param=f"{part_where}.text")
                parts.append(text_value)
            text = "\n".join(parts)
        else:
            raise schema(f"{where}.content must be a string or an array of text parts", param=f"{where}.content")
        if role in ("user", "assistant") and not text.strip():
            raise schema(f"{where}.content must not be empty", param=f"{where}.content")
        out.append(ChatMessage(role=str(role), text=text))
    return tuple(out)


def parse_chat_request(raw: object) -> ParsedChat:
    if not isinstance(raw, dict):
        raise schema("request body must be a JSON object")
    body: dict[str, object] = {}
    stripped: set[str] = set()
    for key, value in raw.items():
        if not isinstance(key, str):
            raise schema("request keys must be strings")
        if key in ACCEPTED_FIELDS:
            body[key] = value
        elif key in STRIPPED_SAMPLING:
            if value is not None:
                stripped.add(key)
        elif key in IGNORED_NEVER_RECORDED:
            continue
        elif key == "n":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value != 1):
                raise unsupported("n > 1 is not supported", param="n")
        elif key == "logprobs":
            if value not in (None, False):
                raise unsupported("logprobs is not supported", param="logprobs")
        else:
            raise unsupported(f"field {key!r} is not supported", param=key)
    try:
        parsed = _Body.model_validate(body)
    except ValidationError as err:
        loc, msg = _first_error(err)
        raise schema(f"{loc}: {msg}" if loc else msg, param=loc or None) from err

    if (
        parsed.max_tokens is not None
        and parsed.max_completion_tokens is not None
        and parsed.max_tokens != parsed.max_completion_tokens
    ):
        raise schema("max_tokens and max_completion_tokens disagree", param="max_completion_tokens")
    max_tokens = parsed.max_tokens if parsed.max_tokens is not None else parsed.max_completion_tokens

    stop: tuple[str, ...]
    if parsed.stop is None:
        stop = ()
    elif isinstance(parsed.stop, str):
        stop = (parsed.stop,)
    else:
        stop = tuple(parsed.stop)
    if len(stop) > MAX_STOP_SEQUENCES:
        raise schema(f"stop accepts at most {MAX_STOP_SEQUENCES} sequences", param="stop")
    if any(not item for item in stop):
        raise schema("stop sequences must be non-empty strings", param="stop")

    response_format = "text"
    json_schema: dict[str, object] | None = None
    if parsed.response_format is not None:
        response_format = parsed.response_format.type
        if response_format == "json_schema":
            if parsed.response_format.json_schema is None:
                raise schema(
                    "response_format.json_schema is required for type json_schema",
                    param="response_format.json_schema",
                )
            json_schema = parsed.response_format.json_schema.schema_
        elif parsed.response_format.json_schema is not None:
            raise schema("response_format.json_schema is only valid with type json_schema", param="response_format")

    return ParsedChat(
        model=parsed.model,
        messages=_parse_messages(parsed.messages),
        max_tokens=max_tokens,
        stop=stop,
        response_format=response_format,
        json_schema=json_schema,
        reasoning_effort=parsed.reasoning_effort,
        stream=parsed.stream,
        ignored=tuple(name for name in STRIPPED_SAMPLING if name in stripped),
    )


@dataclass(frozen=True)
class Translated:
    request: UpstreamRequest
    model: Model


def translate(parsed: ParsedChat, registry: Registry, project: Project) -> Translated:
    name = parsed.model or project.default_model
    model = resolve_model(registry, name)
    if model is None:
        raise GatewayError("E_MODEL_UNKNOWN", 400, f"unknown model {name!r}", param="model")
    if model.id not in project.models:
        raise forbidden(f"model {model.id} is not granted to project {project.id}")

    max_tokens = parsed.max_tokens if parsed.max_tokens is not None else model.default_max_tokens
    if max_tokens > model.max_tokens:
        # Never clamped (spec §3): a bound the caller did not ask for is a silent truncation.
        raise schema(
            f"max_tokens {max_tokens} exceeds the model's max_tokens {model.max_tokens}; caps are never clamped",
            param="max_tokens",
        )

    system_parts = [m.text for m in parsed.messages if m.role in ("system", "developer")]
    turns = tuple((m.role, m.text) for m in parsed.messages if m.role in ("user", "assistant"))
    if not turns:
        raise schema("messages must include at least one user or assistant message", param="messages")
    if turns[0][0] != "user":
        raise schema("the first non-system message must be a user turn", param="messages")
    if parsed.response_format == "json_object":
        system_parts.append(JSON_OBJECT_INSTRUCTION)
    if parsed.reasoning_effort is not None and not model.effort:
        raise unsupported(f"model {model.id} does not accept reasoning_effort", param="reasoning_effort")

    request = UpstreamRequest(
        model=model.id,
        system="\n\n".join(system_parts) or None,
        messages=turns,
        max_tokens=max_tokens,
        stop_sequences=parsed.stop,
        effort=parsed.reasoning_effort,
        json_schema=parsed.json_schema,
    )
    return Translated(request=request, model=model)


# ----------------------------------------------------------------- responses


def finish_reason(stop_reason: str | None) -> str:
    if stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return "length"
    if stop_reason == "refusal":
        return "content_filter"
    return "stop"


def openai_usage(usage: TokenUsage) -> dict[str, object]:
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
    return {
        "prompt_tokens": prompt,
        "completion_tokens": usage.output_tokens,
        "total_tokens": prompt + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cache_read_tokens},
    }


def completion_body(
    *,
    request_id: str,
    created: int,
    model: str,
    text: str,
    stop_reason: str | None,
    usage: TokenUsage,
    gateway: dict[str, object],
) -> dict[str, object]:
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": None},
                "finish_reason": finish_reason(stop_reason),
                "logprobs": None,
            }
        ],
        "usage": openai_usage(usage),
        "gateway": gateway,
    }


def stream_payloads(
    *,
    request_id: str,
    created: int,
    model: str,
    text: str,
    stop_reason: str | None,
    usage: TokenUsage,
    gateway: dict[str, object],
) -> tuple[str, str]:
    """The buffered stream (spec §3): one content chunk, then the final chunk with usage."""
    base: dict[str, object] = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    content = {
        **base,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
    }
    final = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason(stop_reason)}],
        "usage": openai_usage(usage),
        "gateway": gateway,
    }
    return json.dumps(content, separators=(",", ":")), json.dumps(final, separators=(",", ":"))
