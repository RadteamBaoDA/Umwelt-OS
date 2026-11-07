"""Unit tests for the rich web answer: image search, thumbnail proxy allowlist, render_answer, prompt, router."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from core.model_gateway.schemas import WebSearchTarget
from modules.chat import media
from modules.chat import web_search as ws
from modules.chat._worker.finalize import _tool_citation_numbers
from modules.chat.generative_ui import external_router as viz
from modules.chat.generative_ui.prompt import build_generative_ui_system_prompt
from modules.chat.generative_ui.streaming import finalize_generative_ui, streaming_snapshot
from modules.chat.generative_ui.tool_specs import (
    TERMINAL_UI_TOOLS,
    TOOL_ANSWER,
    RenderAnswerArgs,
    build_chat_tool_specs,
    parse_ui_tool_args,
)
from modules.chat.schemas import parse_generative_ui

JSON_H = {"content-type": "application/json"}
BING = "https://tse1.mm.bing.net/th?id=OIP.abc&pid=Api&w=300&h=200"
BRAVE = "https://imgs.search.brave.com/xyz/rs:fit:200:200/g:ce/aHR0cHM"
WIKI = "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/300px-X.jpg"


def _hit(thumb: Any, page: str, title: Any = "A title") -> dict[str, Any]:
    return {
        "thumbnail_src": thumb,
        "url": page,
        "title": title,
        "img_src": "https://evil.example/x.jpg",
    }


# --- image search -------------------------------------------------------------------------------


def test_parse_images_filters_and_dedupes() -> None:
    payload = {
        "results": [
            _hit(BING, "https://a.example/1", "<b>One</b> [x]"),
            _hit(BRAVE, "https://b.example/2"),
            _hit(WIKI, "https://a.example/1"),  # duplicate source page
            _hit("https://evil.example/t.jpg", "https://c.example/3"),  # host not allowlisted
            _hit("http://tse1.mm.bing.net/th?id=1", "https://d.example/4"),  # not https
            _hit(None, "https://e.example/5"),  # malformed
            _hit(BING, "javascript:alert(1)"),  # bad page url
            _hit(BING, "https://f.example/6", ""),  # empty title
            "junk",
            _hit(WIKI, "https://g.example/7"),
        ]
    }
    images = ws.parse_images("searxng", payload)
    assert [i.source_host for i in images] == ["a.example", "b.example", "g.example"]
    assert images[0].url == BING and images[0].description == "One (x)"
    assert all(i.url.startswith("https://") for i in images)


def test_parse_images_cap_and_shape() -> None:
    many = {"results": [_hit(BING, f"https://h.example/{n}") for n in range(30)]}
    assert len(ws.parse_images("searxng", many)) == ws.MAX_IMAGES
    with pytest.raises(ws.WebSearchError):
        ws.parse_images("searxng", {"nope": 1})


def _targets() -> list[WebSearchTarget]:
    return [
        WebSearchTarget(
            provider="omniroute",
            endpoint="https://o.example/v1",
            api_key="k",
            credential_configured=True,
        ),
        WebSearchTarget(
            provider="searxng",
            endpoint="https://s1.example",
            api_key="",
            credential_configured=False,
        ),
        WebSearchTarget(
            provider="searxng",
            endpoint="https://s2.example",
            api_key="",
            credential_configured=False,
        ),
    ]


def test_image_fallback_skips_incapable_and_retries_next() -> None:
    calls: list[tuple[str, str]] = []

    def make_transport(target: WebSearchTarget) -> httpx.AsyncBaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.host or "", request.url.params.get("categories", "")))
            if request.url.host == "s1.example":
                return httpx.Response(503, headers=JSON_H)
            return httpx.Response(
                200, headers=JSON_H, json={"results": [_hit(BING, "https://p.example/1")]}
            )

        return httpx.MockTransport(handler)

    images = asyncio.run(ws.search_images_with_fallback(_targets(), "gta 6", make_transport))
    assert [i.url for i in images] == [BING]
    assert calls == [("s1.example", "images"), ("s2.example", "images")]  # omniroute never called


def test_image_total_failure_is_empty_not_raised() -> None:
    def make_transport(target: WebSearchTarget) -> httpx.AsyncBaseTransport:
        if target.endpoint == "https://s1.example":
            raise ws.EndpointNetworkPolicyError("denied")
        return httpx.MockTransport(lambda request: httpx.Response(500, headers=JSON_H))

    assert asyncio.run(ws.search_images_with_fallback(_targets(), "q", make_transport)) == []
    assert asyncio.run(ws.search_images_with_fallback(_targets()[:1], "q", make_transport)) == []


def test_format_web_images_lists_ids_not_urls() -> None:
    block = ws.format_web_images([ws.WebImage(BING, "GTA", "a.example")])
    assert "img1 | GTA | a.example" in block and "bing.net" not in block


# --- proxy allowlist ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", [BING, BRAVE, WIKI, "https://ts12.mm.bing.net/th?id=a&pid=b"])
def test_proxy_accepts_search_thumbnails_and_keeps_query(url: str) -> None:
    token = media.sign_media_url("secret", url, subject="w:1")
    assert media.verify_media_token("secret", token, subject="w:1") == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/t.jpg",
        "https://bing.net/th?id=1",
        "https://tse1.mm.bing.net.evil.example/th",
        "https://xts1.mm.bing.net/th",
        "http://tse1.mm.bing.net/th",
        "https://tse1.mm.bing.net:8443/th",
        "https://u:p@imgs.search.brave.com/x",
        "https://tse1.mm.bing.net/th#frag",
    ],
)
def test_proxy_rejects_other_hosts(url: str) -> None:
    with pytest.raises(media.MediaError):
        media.sign_media_url("secret", url)


# --- render_answer ------------------------------------------------------------------------------


def _answer(blocks: list[dict[str, Any]], title: str = "GTA VI") -> str:
    return json.dumps({"title": title, "blocks": blocks})


IMAGES = {
    "img1": {"src": "/api/v1/media/proxy?token=t1", "alt": "One", "source_host": "a.example"},
    "img2": {"src": "/api/v1/media/proxy?token=t2", "alt": "Two", "source_host": "b.example"},
}


def test_render_answer_validation() -> None:
    assert TOOL_ANSWER in TERMINAL_UI_TOOLS
    ok = parse_ui_tool_args(TOOL_ANSWER, _answer([{"type": "note", "markdown": "x"}]))
    assert isinstance(ok, RenderAnswerArgs)
    bad_inputs = [
        _answer([{"type": "carousel"}]),
        _answer([]),
        _answer([{"type": "note", "markdown": "x"}] * 13),
        _answer([{"type": "stats", "items": [{"label": "a", "value": "1"}]}]),  # needs 2..4
        _answer([{"type": "gallery", "image_ids": []}]),
        _answer([{"type": "gallery", "image_ids": ["img1"] * 9}]),
        _answer([{"type": "status", "label": "x", "tone": "loud"}]),
        _answer([{"type": "note", "markdown": "x", "extra": 1}]),
        _answer([{"type": "note", "markdown": "x"}], title="t" * 161),
    ]
    for raw in bad_inputs:
        with pytest.raises(ValidationError):
            parse_ui_tool_args(TOOL_ANSWER, raw)


def test_table_rows_are_padded_and_trimmed() -> None:
    args = parse_ui_tool_args(
        TOOL_ANSWER,
        _answer([{"type": "table", "columns": ["a", "b"], "rows": [["1"], ["1", "2", "3"]]}]),
    )
    assert isinstance(args, RenderAnswerArgs)
    assert args.blocks[0].rows == [["1", ""], ["1", "2"]]  # type: ignore[union-attr]


def test_finalize_resolves_images_and_renumbers_citations() -> None:
    raw = _answer(
        [
            {"type": "gallery", "image_ids": ["img2", "img9", "img1"]},
            {"type": "gallery", "image_ids": ["img9"]},  # nothing resolves -> block dropped
            {
                "type": "status",
                "label": "Confirmed",
                "tone": "positive",
                "updated_at": "2026-11-07",
            },
            {"type": "key_value", "value": "19/11/2026", "caption": "Release", "citations": [3, 7]},
            {
                "type": "stats",
                "items": [
                    {"label": "a", "value": "1", "citations": [3]},
                    {"label": "b", "value": "2", "citations": [7]},
                ],
            },
            {
                "type": "section",
                "heading": "H",
                "markdown": "m [3]",
                "image_ids": ["img1", "img1", "nope"],
                "citations": [3],
            },
        ]
    )
    payload = finalize_generative_ui(
        TOOL_ANSWER, raw, {}, False, images=IMAGES, number_map={3: 1, 5: 2}
    )
    assert payload is not None and payload["kind"] == "answer"
    blocks = payload["answer"]["blocks"]
    assert [b["type"] for b in blocks] == ["gallery", "status", "key_value", "stats", "section"]
    assert [i["src"] for i in blocks[0]["images"]] == [IMAGES["img2"]["src"], IMAGES["img1"]["src"]]
    assert blocks[2]["citations"] == [1]  # 7 is unmapped and dropped
    assert [i["citations"] for i in blocks[3]["items"]] == [[1], []]
    assert blocks[4]["images"] == [IMAGES["img1"]] and "image_ids" not in blocks[4]
    assert parse_generative_ui(payload) is not None  # server-side schema accepts it


def test_finalize_returns_none_when_nothing_survives() -> None:
    raw = _answer([{"type": "gallery", "image_ids": ["img9"]}])
    assert finalize_generative_ui(TOOL_ANSWER, raw, {}, False, images=IMAGES) is None
    assert finalize_generative_ui(TOOL_ANSWER, "{}", {}, False) is None


def test_streaming_snapshot_hides_ids_and_citations() -> None:
    snap = streaming_snapshot(
        TOOL_ANSWER,
        '{"title":"T","blocks":[{"type":"gallery","image_ids":["img1"]},{"type":"key_value","value":"v","citations":[1]},{"type":"sec',
    )
    assert snap is not None and snap["kind"] == "answer"
    blocks = snap["answer"]["blocks"]
    assert blocks[0] == {"type": "gallery", "images": []}
    assert blocks[1] == {"type": "key_value", "value": "v"}


def test_tool_citation_numbers_reads_nested_blocks() -> None:
    call = {
        "arguments": _answer(
            [
                {"type": "key_value", "value": "v", "citations": [1, 4]},
                {"type": "stats", "items": [{"label": "a", "value": "1", "citations": [5]}] * 2},
            ]
        )
    }
    assert _tool_citation_numbers(call, 5) == [1, 4, 5, 5]
    assert _tool_citation_numbers(call, 5, lowest=4) == [4, 5, 5]


def test_tool_gating_prompt_and_router() -> None:
    names = {s["function"]["name"] for s in build_chat_tool_specs(include_read_skill=False)}
    assert TOOL_ANSWER not in names
    names = {
        s["function"]["name"]
        for s in build_chat_tool_specs(include_read_skill=False, include_answer=True)
    }
    assert TOOL_ANSWER in names
    assert "Rich answers" not in build_generative_ui_system_prompt(locale="en-US")
    on = build_generative_ui_system_prompt(locale="en-US", rich_answer=True)
    assert "## Rich answers" in on and "render_answer" in on
    assert "get_trip_stop_images" not in on
    assert "get_trip_stop_images" in build_generative_ui_system_prompt(
        locale="en-US", rich_answer=True, remote_media=True
    )
    assert viz.allowed_presentation_tools("rich_answer") == frozenset({TOOL_ANSWER})
    decision = viz.decision_for("rich_answer", "llm")
    assert decision["kind"] == "answer"
    specs = build_chat_tool_specs(include_read_skill=False, include_answer=True)
    kept = {s["function"]["name"] for s in viz.filter_tool_specs(specs, decision)}
    assert TOOL_ANSWER in kept and "render_table" not in kept and "render_chart" not in kept
    other = viz.decision_for("table", "llm")
    assert TOOL_ANSWER not in {s["function"]["name"] for s in viz.filter_tool_specs(specs, other)}


def test_web_prompt_suffix_lists_images_without_urls() -> None:
    from modules.chat._worker.setup import _web_prompt_suffix
    from modules.chat._worker.web import _web_search_run

    result = ws.WebSearchResult("T", "https://a.example/x", "a.example", "snip")
    run = _web_search_run(None, [result], "searxng")
    image = ws.WebImage(BING, "GTA cover", "b.example")
    with_images = _web_prompt_suffix(run, 0, [image])
    assert "img1 | GTA cover | b.example" in with_images and "bing.net" not in with_images
    assert "Web images" not in _web_prompt_suffix(run, 0)
    assert "provider" not in run.outcome and "fallback" not in run.outcome
