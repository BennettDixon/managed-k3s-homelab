import pytest

from gateway.errors import GatewayError
from gateway.money import TokenUsage
from gateway.openai_compat import (
    JSON_OBJECT_INSTRUCTION,
    completion_body,
    finish_reason,
    openai_usage,
    parse_chat_request,
    stream_payloads,
    translate,
)
from gateway.registry import Project
from tests.conftest import load_test_registry

BASIC = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}


def project(name: str = "homelab-ops") -> Project:
    return load_test_registry().projects[name]


def err(raw: object) -> GatewayError:
    with pytest.raises(GatewayError) as info:
        parse_chat_request(raw)
    return info.value


def test_accepts_the_subset_and_strips_sampling_params() -> None:
    parsed = parse_chat_request(
        {
            **BASIC,
            "temperature": 0.2,
            "top_p": 0.9,
            "presence_penalty": 0,
            "frequency_penalty": None,
            "user": "u1",
            "metadata": {"a": 1},
            "store": False,
            "stream_options": {"include_usage": True},
            "n": 1,
            "logprobs": False,
            "max_completion_tokens": 50,
            "stop": ["END"],
            "reasoning_effort": "high",
            "stream": True,
        }
    )
    assert parsed.ignored == ("temperature", "top_p", "presence_penalty", "stream_options")  # null: not stripped
    assert parsed.max_tokens == 50
    assert parsed.stop == ("END",)
    assert parsed.reasoning_effort == "high"
    assert parsed.stream is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tools", []),
        ("functions", []),
        ("tool_choice", "auto"),
        ("logit_bias", {}),
        ("seed", 1),
        ("modalities", ["text"]),
        ("audio", {}),
        ("prediction", {}),
        ("service_tier", "auto"),
        ("top_logprobs", 1),
        ("parallel_tool_calls", True),
        ("made_up_field", 1),
    ],
)
def test_rejects_by_name(field: str, value: object) -> None:
    e = err({**BASIC, field: value})
    assert e.code == "E_UNSUPPORTED"
    assert e.param == field
    assert e.status == 400


def test_n_and_logprobs_only_in_their_trivial_forms() -> None:
    assert err({**BASIC, "n": 2}).param == "n"
    assert err({**BASIC, "n": True}).param == "n"
    assert err({**BASIC, "n": 1.0}).param == "n"
    assert parse_chat_request({**BASIC, "n": 1}).model == "haiku"
    assert parse_chat_request({**BASIC, "n": None}).model == "haiku"
    assert err({**BASIC, "logprobs": True}).param == "logprobs"


def test_our_own_reply_round_trips_through_a_stock_client() -> None:
    echoed: dict[str, object] = {
        "role": "assistant",
        "content": "hi",
        "refusal": None,
        "tool_calls": None,
        "function_call": None,
        "annotations": [],
        "audio": None,
    }
    parsed = parse_chat_request(
        {"messages": [{"role": "user", "content": "u"}, echoed, {"role": "user", "content": "v"}]}
    )
    assert [m.role for m in parsed.messages] == ["user", "assistant", "user"]
    e = err({"messages": [{"role": "user", "content": "u"}, {**echoed, "tool_calls": [{"id": "x"}]}]})
    assert e.code == "E_UNSUPPORTED" and e.param == "messages[1].tool_calls"
    assert err({"messages": [{"role": "user", "content": "u", "refusal": None}]}).param == "messages[0].refusal"


def test_strict_types_no_coercion() -> None:
    e = err({**BASIC, "max_tokens": "5"})
    assert e.code == "E_SCHEMA" and e.param == "max_tokens"
    assert err({**BASIC, "max_tokens": 0}).param == "max_tokens"
    assert err({**BASIC, "messages": []}).param == "messages"
    assert err({**BASIC, "stream": "yes"}).param == "stream"
    assert err({**BASIC, "reasoning_effort": "ultra"}).param == "reasoning_effort"
    assert err("nope").code == "E_SCHEMA"


def test_max_tokens_fields_must_agree() -> None:
    assert err({**BASIC, "max_tokens": 5, "max_completion_tokens": 6}).param == "max_completion_tokens"
    assert parse_chat_request({**BASIC, "max_tokens": 5, "max_completion_tokens": 5}).max_tokens == 5


def test_stop_limits() -> None:
    assert parse_chat_request({**BASIC, "stop": "x"}).stop == ("x",)
    assert err({**BASIC, "stop": ["a", "b", "c", "d", "e"]}).param == "stop"
    assert err({**BASIC, "stop": [""]}).param == "stop"


def test_messages_parts_and_roles() -> None:
    parsed = parse_chat_request(
        {
            "messages": [
                {"role": "developer", "content": "be terse"},
                {"role": "system", "content": [{"type": "text", "text": "and kind"}]},
                {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
                {"role": "assistant", "content": "ok"},
            ]
        }
    )
    assert [m.role for m in parsed.messages] == ["developer", "system", "user", "assistant"]
    assert parsed.messages[2].text == "a\nb"
    e = err({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]})
    assert e.code == "E_UNSUPPORTED" and e.param == "messages[0].content[0].type"
    assert err({"messages": [{"role": "tool", "content": "x"}]}).param == "messages[0].role"
    assert err({"messages": [{"role": "user", "content": "x", "name": "n"}]}).param == "messages[0].name"
    assert err({"messages": [{"role": "user", "content": None}]}).param == "messages[0].content"
    assert err({"messages": [{"role": "user", "content": "   "}]}).param == "messages[0].content"
    assert err({"messages": [{"role": "robot", "content": "x"}]}).param == "messages[0].role"
    assert err({"messages": ["x"]}).param == "messages[0]"


def test_response_format_shapes() -> None:
    parsed = parse_chat_request({**BASIC, "response_format": {"type": "json_object"}})
    assert parsed.response_format == "json_object"
    parsed = parse_chat_request(
        {
            **BASIC,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True},
            },
        }
    )
    assert parsed.json_schema == {"type": "object"}
    assert err({**BASIC, "response_format": {"type": "json_schema"}}).param == "response_format.json_schema"
    assert (
        err({**BASIC, "response_format": {"type": "text", "json_schema": {"name": "x", "schema": {}}}}).param
        == "response_format"
    )


def test_translate_hoists_system_and_applies_defaults() -> None:
    registry = load_test_registry()
    parsed = parse_chat_request(
        {
            "messages": [
                {"role": "system", "content": "S1"},
                {"role": "user", "content": "u"},
                {"role": "developer", "content": "S2"},
                {"role": "assistant", "content": "a"},
            ],
            "response_format": {"type": "json_object"},
        }
    )
    t = translate(parsed, registry, project())
    assert t.model.id == "claude-sonnet-5"  # project default
    assert t.request.system == "S1\n\nS2\n\n" + JSON_OBJECT_INSTRUCTION
    assert t.request.messages == (("user", "u"), ("assistant", "a"))
    assert t.request.max_tokens == 4096  # model default
    assert t.request.output_config() is None


def test_translate_effort_and_schema_into_output_config() -> None:
    registry = load_test_registry()
    parsed = parse_chat_request(
        {
            "model": "opus",
            "messages": [{"role": "user", "content": "u"}],
            "reasoning_effort": "xhigh",
            "response_format": {"type": "json_schema", "json_schema": {"name": "n", "schema": {"type": "object"}}},
        }
    )
    t = translate(parsed, registry, project())
    assert t.request.model == "claude-opus-5"
    assert t.request.output_config() == {
        "effort": "xhigh",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }


def test_translate_rejections() -> None:
    registry = load_test_registry()

    def tr(body: dict[str, object], proj: str = "homelab-ops") -> GatewayError:
        with pytest.raises(GatewayError) as info:
            translate(parse_chat_request(body), registry, project(proj))
        return info.value

    assert tr({"model": "gpt-4o", "messages": [{"role": "user", "content": "u"}]}).code == "E_MODEL_UNKNOWN"
    e = tr({"model": "opus", "messages": [{"role": "user", "content": "u"}]}, "gateway-smoke")
    assert e.code == "E_FORBIDDEN" and e.status == 403
    e = tr({**BASIC, "max_tokens": 8001})
    assert e.code == "E_SCHEMA" and e.param == "max_tokens" and "never clamped" in e.message
    e = tr({**BASIC, "reasoning_effort": "low"})  # haiku: effort false
    assert e.code == "E_UNSUPPORTED" and e.param == "reasoning_effort"
    e = tr({"messages": [{"role": "assistant", "content": "a"}, {"role": "user", "content": "u"}]})
    assert e.code == "E_SCHEMA" and "user turn" in e.message
    e = tr({"messages": [{"role": "system", "content": "only system"}]})
    assert e.code == "E_SCHEMA"


def test_response_shapes() -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=4, cache_read_tokens=3, cache_write_5m_tokens=2)
    assert openai_usage(usage) == {
        "prompt_tokens": 15,
        "completion_tokens": 4,
        "total_tokens": 19,
        "prompt_tokens_details": {"cached_tokens": 3},
    }
    assert finish_reason("end_turn") == "stop"
    assert finish_reason("stop_sequence") == "stop"
    assert finish_reason("max_tokens") == "length"
    assert finish_reason("refusal") == "content_filter"
    assert finish_reason("model_context_window_exceeded") == "length"
    assert finish_reason(None) == "stop"
    body = completion_body(
        request_id="01ABC", created=1, model="m", text="hi", stop_reason="max_tokens", usage=usage, gateway={"x": 1}
    )
    assert body["id"] == "chatcmpl-01ABC"
    assert body["choices"][0]["finish_reason"] == "length"  # type: ignore[index]
    assert body["choices"][0]["message"]["content"] == "hi"  # type: ignore[index]
    first, last = stream_payloads(
        request_id="01ABC", created=1, model="m", text="hi", stop_reason="end_turn", usage=usage, gateway={"x": 1}
    )
    assert '"chat.completion.chunk"' in first and '"content":"hi"' in first
    assert '"finish_reason":"stop"' in last and '"gateway":{"x":1}' in last
