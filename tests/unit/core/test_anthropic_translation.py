"""Pure OpenAI <-> Anthropic Messages translation."""

from __future__ import annotations

import pytest

from core.model_gateway.anthropic import (
    StreamTranslator,
    from_messages_response,
    to_messages_request,
)
from core.model_gateway.client import CapabilityUnsupported, ModelGatewayError


def test_system_tools_and_tool_results_translate() -> None:
    req = to_messages_request(
        {
            "model": "claude-sonnet-5-5",
            "messages": [
                {"role": "system", "content": "S1"},
                {"role": "system", "content": "S2"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_details": [{"type": "thinking", "thinking": "", "signature": "sig"}],
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"a":1}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "toolu_1", "content": "42"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "description": "d",
                        "parameters": {"type": "object", "properties": {"a": {"type": "integer"}}},
                    },
                }
            ],
            "temperature": 0.2,
        },
        stream=False,
    )
    assert req["system"] == "S1\n\nS2" and req["max_tokens"] == 4096 and "temperature" not in req
    assert req["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}},
    ]
    assert req["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"}],
    }
    assert req["tools"] == [
        {
            "name": "f",
            "description": "d",
            "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}},
        }
    ]


def test_foreign_tool_ids_are_sanitised_consistently() -> None:
    req = to_messages_request(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call.1:x", "function": {"name": "f", "arguments": "bad"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call.1:x", "content": "r"},
            ],
        },
        stream=False,
    )
    use, result = req["messages"][0]["content"][0], req["messages"][1]["content"][0]
    assert use["id"] == result["tool_use_id"] == "call_1_x" and use["input"] == {}


def test_images_translate() -> None:
    req = to_messages_request(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "t"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    ],
                }
            ],
        },
        stream=False,
    )
    assert req["messages"][0]["content"][1:] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
    ]


def test_forced_tool_choice_maps_to_anthropic_any_and_tool() -> None:
    """``required`` becomes ``any`` and a named function becomes ``tool``; ``none`` stays ``none``."""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": tools}
    named = to_messages_request(
        {**base, "tool_choice": {"type": "function", "function": {"name": "f"}}}, stream=False
    )
    assert named["tool_choice"] == {"type": "tool", "name": "f"}
    assert to_messages_request({**base, "tool_choice": "required"}, stream=False)[
        "tool_choice"
    ] == {"type": "any"}
    assert "tool_choice" not in to_messages_request({**base, "tool_choice": "auto"}, stream=False)
    assert to_messages_request({**base, "tool_choice": "none"}, stream=False)["tool_choice"] == {
        "type": "none"
    }


def test_forced_tool_choice_that_cannot_be_honoured_raises_capability_unsupported() -> None:
    """No tools, or extended thinking on: raise so the router falls back visibly instead of running unforced."""
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}], "tool_choice": "required"}
    with pytest.raises(CapabilityUnsupported):
        to_messages_request(base, stream=False)
    tools = [{"type": "function", "function": {"name": "f"}}]
    with pytest.raises(CapabilityUnsupported, match="thinking"):
        to_messages_request({**base, "tools": tools, "reasoning_effort": "high"}, stream=False)


def test_tool_message_with_text_parts_is_flattened_not_json_dumped() -> None:
    """A list-of-parts tool message becomes plain text; other shapes are still JSON-encoded."""

    def result(content: object) -> object:
        out = to_messages_request(
            {
                "model": "m",
                "messages": [{"role": "tool", "tool_call_id": "c1", "content": content}],
            },
            stream=False,
        )
        return out["messages"][0]["content"][0]["content"]

    assert result([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert result("plain") == "plain"
    assert result({"k": 1}) == '{"k": 1}'


def test_user_turn_without_usable_content_is_dropped() -> None:
    """Empty string, empty list and unusable parts would be a 400 (empty content): the turn is omitted."""
    messages = [
        {"role": "user", "content": ""},
        {"role": "user", "content": []},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "ftp://x"}}]},
        {"role": "user", "content": "kept"},
    ]
    out = to_messages_request({"model": "m", "messages": messages}, stream=False)
    assert out["messages"] == [{"role": "user", "content": "kept"}]


def test_structured_output_and_effort_map_to_output_config() -> None:
    req = to_messages_request(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "reasoning_effort": "high",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "q", "strict": True, "schema": {"type": "object"}},
            },
        },
        stream=True,
    )
    assert req["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}},
        "effort": "high",
    }
    assert (
        req["thinking"] == {"type": "adaptive", "display": "summarized"} and req["stream"] is True
    )


def test_json_object_response_format_is_dropped() -> None:
    req = to_messages_request(
        {"model": "m", "messages": [], "response_format": {"type": "json_object"}}, stream=False
    )
    assert "output_config" not in req


def test_response_maps_text_tools_thinking_stop_and_usage() -> None:
    out = from_messages_response(
        {
            "id": "msg_1",
            "model": "m",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "hm", "signature": "s"},
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}},
            ],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 5},
        }
    )
    msg = out["choices"][0]["message"]
    assert msg["content"] == "ok" and msg["reasoning_content"] == "hm"
    assert msg["tool_calls"][0]["function"] == {"name": "f", "arguments": '{"a": 1}'}
    assert msg["reasoning_details"] == [{"type": "thinking", "thinking": "hm", "signature": "s"}]
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def test_stream_translates_text_tool_deltas_thinking_and_usage() -> None:
    tr, chunks = StreamTranslator(), []
    for ev in [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 3}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "t"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sg"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_9", "name": "f", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"a"'},
        },
        {"type": "ping"},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]:
        chunks += tr.feed(ev)
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert {"reasoning_content": "t"} in deltas and {"content": "Hi"} in deltas
    assert {
        "tool_calls": [
            {
                "index": 0,
                "id": "toolu_9",
                "type": "function",
                "function": {"name": "f", "arguments": ""},
            }
        ]
    } in deltas
    assert {"tool_calls": [{"index": 0, "function": {"arguments": '{"a"'}}]} in deltas
    assert any(
        d.get("reasoning_details") == [{"type": "thinking", "thinking": "t", "signature": "sg"}]
        for d in deltas
    )
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}


def test_stream_error_event_raises() -> None:
    with pytest.raises(ModelGatewayError, match="overloaded_error"):
        StreamTranslator().feed(
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        )
