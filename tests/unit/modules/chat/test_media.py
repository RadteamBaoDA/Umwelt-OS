"""Unit tests for opt-in widget remote media: trip images tool and signed proxy (mock transports only)."""

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from core.model_gateway.schemas import AIExecutionConfig, ModelDefaults, PrivacySettings
from modules.chat import media
from modules.chat.generative_ui.prompt import build_generative_ui_system_prompt
from modules.chat.generative_ui.tool_specs import TOOL_TRIP_IMAGES, build_chat_tool_specs

SECRET = "unit-test-secret"
THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Eiffel.jpg/320px-Eiffel.jpg"
JSON_H = {"content-type": "application/json"}


def _config(flag: bool = True) -> AIExecutionConfig:
    return AIExecutionConfig(
        workspace_id=uuid4(), actor_user_id=1, membership_revision=1, access_configuration_revision=1,
        configuration_revision=1, gateway_identity="a" * 64, endpoint_destination_id=None, gateway_base_url=None,
        gateway_api_key="", defaults=ModelDefaults(), privacy=PrivacySettings(allow_widget_remote_media=flag),
        request_timeout_seconds=20, web_search_provider="none", web_search_endpoint=None,
        web_search_api_key="",
    )


def _factory(handler: Any) -> Any:
    return lambda _host: httpx.MockTransport(handler)


def _wiki(source: str = THUMB) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "en.wikipedia.org":
            return httpx.Response(200, json={"thumbnail": {"source": source}}, headers=JSON_H)
        meta = {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": "<a href='x'>Jane</a>"}}
        page = {"imageinfo": [{"extmetadata": meta}]}
        return httpx.Response(200, json={"query": {"pages": [page]}}, headers=JSON_H)
    return handler


def _images(handler: Any, titles: list[str] | None = None) -> list[dict[str, str]]:
    return asyncio.run(media.get_trip_stop_images(titles or ["Eiffel Tower"], secret=SECRET, factory=_factory(handler)))


def test_ok_returns_proxied_url_and_credit() -> None:
    [stop] = _images(_wiki())
    assert stop["status"] == "ok" and stop["credit"] == "Jane" and stop["license"] == "CC BY-SA 4.0"
    assert stop["image_url"].startswith(media.PROXY_PATH + "?token=")
    assert media.verify_media_token(SECRET, stop["image_url"].split("token=")[1]) == THUMB
    assert stop["article_url"] == "https://en.wikipedia.org/wiki/Eiffel_Tower"


def test_license_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "en.wikipedia.org":
            return _wiki()(request)
        meta = {"LicenseShortName": {"value": "CC BY-NC 4.0"}, "Artist": {"value": "Jane"}}
        return httpx.Response(200, json={"query": {"pages": [{"imageinfo": [{"extmetadata": meta}]}]}}, headers=JSON_H)
    assert _images(handler) == [{"title": "Eiffel Tower", "status": "unavailable"}]


def test_missing_credit_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "en.wikipedia.org":
            return _wiki()(request)
        meta = {"LicenseShortName": {"value": "CC0"}}
        return httpx.Response(200, json={"query": {"pages": [{"imageinfo": [{"extmetadata": meta}]}]}}, headers=JSON_H)
    assert _images(handler)[0]["status"] == "unavailable"


def test_host_rejected() -> None:
    assert _images(_wiki("https://evil.example/wikipedia/commons/a/ab/X.jpg"))[0]["status"] == "unavailable"


def test_redirect_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/"})
    assert _images(handler)[0]["status"] == "unavailable"


def test_flag_off_and_local_only_refused() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no egress expected")
    args = json.dumps({"titles": ["Eiffel Tower"]})
    for config, local in ((_config(False), False), (_config(True), True)):
        out = asyncio.run(media.run_trip_images_tool(args, config=config, local_only=local, secret=SECRET, factory=_factory(boom)))
        assert json.loads(out) == {"error": "disabled"}


def test_tool_validates_title_count() -> None:
    bad = json.dumps({"titles": [f"T{i}" for i in range(9)]})
    out = asyncio.run(media.run_trip_images_tool(bad, config=_config(), local_only=False, secret=SECRET))
    assert json.loads(out) == {"error": "invalid_arguments"}


def test_tool_ok_end_to_end() -> None:
    out = asyncio.run(media.run_trip_images_tool(json.dumps({"titles": ["Eiffel Tower"]}), config=_config(),
                                                 local_only=False, secret=SECRET, factory=_factory(_wiki())))
    assert json.loads(out)["stops"][0]["status"] == "ok"


def test_token_tamper_expiry_and_host() -> None:
    token = media.sign_media_url(SECRET, THUMB, now=1000)
    assert media.verify_media_token(SECRET, token, now=1001) == THUMB
    payload, sig = token.split(".")
    for bad, code in ((f"{payload}.{'0' * 64}", "bad_token"), (f"x{payload}.{sig}", "bad_token"), ("garbage", "bad_token")):
        with pytest.raises(media.MediaError) as err:
            media.verify_media_token(SECRET, bad, now=1001)
        assert err.value.code == code
    with pytest.raises(media.MediaError) as err:
        media.verify_media_token(SECRET, token, now=1000 + media.TOKEN_TTL_SECONDS + 1)
    assert err.value.code == "expired"
    with pytest.raises(media.MediaError):
        media.verify_media_token("other-secret", token, now=1001)
    for url in ("https://evil.example/a.jpg", "http://upload.wikimedia.org/a.jpg", "https://upload.wikimedia.org:8443/a.jpg",
                "https://user@upload.wikimedia.org/a.jpg"):
        with pytest.raises(media.MediaError):
            media.sign_media_url(SECRET, url)
    with pytest.raises(media.MediaError):
        media.sign_media_url("", THUMB)


def _fetch(response: httpx.Response) -> tuple[bytes, str]:
    return asyncio.run(media.fetch_media(THUMB, _factory(lambda request: response)))


def test_fetch_ok_and_rejections() -> None:
    assert _fetch(httpx.Response(200, content=b"png", headers={"content-type": "image/png"})) == (b"png", "image/png")
    for response in (
        httpx.Response(200, content=b"x", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
        httpx.Response(302, headers={"location": "https://upload.wikimedia.org/x"}),
        httpx.Response(404, headers={"content-type": "image/png"}),
        httpx.Response(200, content=b"x" * (media.MAX_MEDIA_BYTES + 1), headers={"content-type": "image/png"}),
    ):
        with pytest.raises(media.MediaError):
            _fetch(response)
    with pytest.raises(media.MediaError):
        asyncio.run(media.fetch_media("https://evil.example/a.png", _factory(lambda r: httpx.Response(200))))


def test_tile_url_bounds() -> None:
    assert media.tile_url(2, 1, 3) == "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/2/3/1"
    for z, x, y in ((20, 0, 0), (2, 4, 0), (-1, 0, 0)):
        with pytest.raises(media.MediaError):
            media.tile_url(z, x, y)


def test_tool_spec_and_prompt_gated() -> None:
    names = lambda specs: {s["function"]["name"] for s in specs}
    assert TOOL_TRIP_IMAGES not in names(build_chat_tool_specs(include_read_skill=False))
    assert TOOL_TRIP_IMAGES in names(build_chat_tool_specs(include_read_skill=False, include_trip_images=True))
    assert TOOL_TRIP_IMAGES not in build_generative_ui_system_prompt(locale="en-US")
    assert TOOL_TRIP_IMAGES in build_generative_ui_system_prompt(locale="en-US", remote_media=True)
    assert TOOL_TRIP_IMAGES not in build_generative_ui_system_prompt(locale="en-US", remote_media=True, local_only=True)


def test_token_is_bound_to_its_subject() -> None:
    token = media.sign_media_url(SECRET, THUMB, now=1000, subject="ws1:7")
    assert media.verify_media_token(SECRET, token, now=1001, subject="ws1:7") == THUMB
    for other in ("ws1:8", "ws2:7", ""):
        with pytest.raises(media.MediaError) as err:
            media.verify_media_token(SECRET, token, now=1001, subject=other)
        assert err.value.code == "bad_token"
    unbound = media.sign_media_url(SECRET, THUMB, now=1000)
    with pytest.raises(media.MediaError):
        media.verify_media_token(SECRET, unbound, now=1001, subject="ws1:7")


def test_tool_signs_with_the_actor_subject() -> None:
    out = asyncio.run(media.run_trip_images_tool(json.dumps({"titles": ["Eiffel Tower"]}), config=_config(),
                                                 local_only=False, secret=SECRET, factory=_factory(_wiki()),
                                                 subject="ws1:7"))
    token = json.loads(out)["stops"][0]["image_url"].split("token=")[1]
    assert media.verify_media_token(SECRET, token, subject="ws1:7") == THUMB
    with pytest.raises(media.MediaError):
        media.verify_media_token(SECRET, token, subject="ws1:8")


def test_media_routes_carry_the_budget_dependency() -> None:
    from modules.chat import media_routes

    calls = {d.dependency.__qualname__ for d in media_routes.router.dependencies}
    assert any("require_budget" in c for c in calls)
