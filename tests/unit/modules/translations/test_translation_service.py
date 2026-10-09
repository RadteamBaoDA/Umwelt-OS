import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core.model_gateway.client import ModelGatewayError, PrivacyPolicyDenied
from modules.translations import service
from modules.translations.schemas import TranslationInput

EN = "The company said that the revenue is up for the quarter and the outlook is good for investors."
POLICY = SimpleNamespace(reasoning_allowed=True)


def _src(rtype="news_story", fields=None, local_only=False):
    fields = fields or {"title": "Revenue up 5%", "excerpt": EN + " Now 10 USD."}
    return TranslationInput(uuid4(), 1, rtype, uuid4(), "r", fields, "v" * 64, (), local_only)


def _cfg(remote=True):
    return SimpleNamespace(
        privacy=SimpleNamespace(allow_remote_reasoning=remote), aliases={"reasoning-small": object()})


def _reply(content):
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


def _echo(text):
    return "VI " + text  # keeps markers, order and line structure


async def _run(gw, src=None, cfg=None, before=None, deadline=None):
    return await service.translate_input(
        gw, cfg or _cfg(), POLICY, src or _src(), "vi", before or AsyncMock(),
        deadline=deadline or time.monotonic() + 30)


async def test_news_exact_schema_and_before_send_passthrough():
    seen = {}

    async def structured(alias, mapping, policy, messages, schema, **kw):
        seen.update(schema=schema, kw=kw)
        payload = json.loads(messages[1]["content"])
        return _reply({k: _echo(v) for k, v in payload.items()})

    before = AsyncMock()
    out = await _run(SimpleNamespace(structured=structured), before=before)
    assert out["title"] == "VI Revenue up 5%"
    assert out["excerpt"].endswith("Now 10 USD.")
    assert seen["schema"]["schema"]["required"] == ["title", "excerpt"]
    assert seen["schema"]["schema"]["additionalProperties"] is False
    assert seen["kw"] == {"max_tokens": 4096, "temperature": 0, "before_send": before}


async def test_extra_field_rejected():
    gw = SimpleNamespace(structured=AsyncMock(return_value=_reply({"title": "a", "excerpt": "b", "x": "c"})))
    with pytest.raises(service.TranslationBlocked) as exc:
        await _run(gw)
    assert exc.value.code == "invalid_model_output"


async def test_invented_number_rejected():
    async def structured(alias, mapping, policy, messages, schema, **kw):
        payload = json.loads(messages[1]["content"])
        payload["title"] += " 99"
        return _reply(payload)

    with pytest.raises(service.TranslationBlocked):
        await _run(SimpleNamespace(structured=structured))


async def test_already_target_language_skips_model():
    gw = SimpleNamespace(structured=AsyncMock())
    src = _src(fields={"title": "Doanh thu tăng",
                       "excerpt": "Công ty cho biết doanh thu quý này tăng mạnh nhờ nhu cầu tốt hơn."})
    assert await _run(gw, src) == src.fields
    gw.structured.assert_not_called()


@pytest.mark.parametrize(("cfg", "src"), [(_cfg(False), _src()), (_cfg(), _src(local_only=True))])
async def test_privacy_blocks_before_send(cfg, src):
    gw = SimpleNamespace(structured=AsyncMock())
    with pytest.raises(service.TranslationBlocked) as exc:
        await _run(gw, src, cfg=cfg)
    assert exc.value.code == "privacy_blocked"
    gw.structured.assert_not_called()


async def test_gateway_errors_map_without_retry():
    cases = ((ModelGatewayError("Model capability is not supported"), "model_capability_missing"),
             (PrivacyPolicyDenied("x"), "privacy_blocked"))
    for err, code in cases:
        gw = SimpleNamespace(structured=AsyncMock(side_effect=err))
        with pytest.raises(service.TranslationBlocked) as exc:
            await _run(gw)
        assert exc.value.code == code
        assert gw.structured.await_count == 1


async def test_size_limits():
    gw = SimpleNamespace(structured=AsyncMock())
    with pytest.raises(service.TranslationBlocked) as exc:
        await _run(gw, _src(fields={"title": "x" * 501, "excerpt": ""}))
    assert exc.value.code == "input_too_large"
    with pytest.raises(service.TranslationBlocked) as exc:
        await _run(gw, _src("daily_brief", {"content": "word\n" * 8001}))
    assert exc.value.code == "input_too_large"


async def test_brief_twenty_segment_cap():
    gw = SimpleNamespace(structured=AsyncMock())
    with pytest.raises(service.TranslationBlocked) as exc:
        await _run(gw, _src("daily_brief", {"content": "\n".join(["w" * 1999] * 21)}))
    assert exc.value.code == "input_too_large"
    gw.structured.assert_not_called()


async def test_brief_deadline_gives_no_partial_result(monkeypatch):
    src = _src("daily_brief", {"content": "\n".join(["w" * 1999] * 3)})
    calls = []

    async def structured(alias, mapping, policy, messages, schema, **kw):
        calls.append(1)
        return _reply({"content": json.loads(messages[1]["content"])["content"]})

    clock = iter([0.0, 0.0, 1e9])  # second segment sees an expired deadline
    monkeypatch.setattr(service.time, "monotonic", lambda: next(clock, 1e9))
    with pytest.raises(service.TranslationBlocked) as exc:
        await service.translate_input(
            SimpleNamespace(structured=structured), _cfg(), POLICY, src, "vi", AsyncMock(), deadline=500)
    assert exc.value.code == "deadline_exceeded"
    assert len(calls) == 1


async def test_brief_roundtrip_keeps_structure():
    src = _src("daily_brief", {"content": "# Heading\n- gain 5% [1]\n\nplain " + EN})

    async def structured(alias, mapping, policy, messages, schema, **kw):
        assert schema["schema"]["required"] == ["content"]
        masked = json.loads(messages[1]["content"])["content"]
        return _reply({"content": "\n".join(_echo(x) for x in masked.split("\n"))})

    out = await _run(SimpleNamespace(structured=structured), src)
    assert out["content"].startswith("# VI Heading\n- VI gain 5% [1]\n\nVI plain")


def test_content_hash_depends_on_fields():
    a, b = _src(), _src(fields={"title": "x", "excerpt": "y"})
    assert service.content_hash(a) != service.content_hash(b)
    assert service.content_hash(a) == service.content_hash(a)


def test_fingerprint_is_order_independent_and_part_sensitive():
    from modules.translations.public import PROMPT_VERSION, translation_fingerprint
    assert PROMPT_VERSION == "v2"
    assert translation_fingerprint({"a": 1, "b": "x"}) == translation_fingerprint({"b": "x", "a": 1})
    assert translation_fingerprint({"a": 1}) != translation_fingerprint({"a": 2})


async def test_inputs_adapter_registers_and_rejects_foreign_workspace(monkeypatch):
    from types import ModuleType

    from modules.translations import inputs, public
    src = _src()
    mod = ModuleType("fake_news")
    mod.read_story_translation_input = AsyncMock(return_value=src)
    monkeypatch.setattr(inputs, "import_module", lambda name: mod)
    scope = SimpleNamespace(workspace_id=src.workspace_id)
    verdict = await public._AUTHORIZERS["news_story"](None, scope=scope, resource_id=src.resource_id,
                                                      multi_workspace_enabled=False)
    assert verdict.content_hash == service.content_hash(src) and verdict.resource_revision == "r"
    other = SimpleNamespace(workspace_id=uuid4())
    assert await public._AUTHORIZERS["news_story"](None, scope=other, resource_id=src.resource_id,
                                                   multi_workspace_enabled=False) is None


async def test_token_use_is_counted_with_bounded_labels(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "count", lambda name, value=1, /, **labels: calls.append((name, value, labels)))

    async def structured(alias, mapping, policy, messages, schema, **kw):
        payload = json.loads(messages[1]["content"])
        return {**_reply({k: _echo(v) for k, v in payload.items()}), "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    await _run(SimpleNamespace(structured=structured))
    assert calls == [("translation_tokens_total", 7, {"direction": "in", "target_language": "vi"}),
                     ("translation_tokens_total", 3, {"direction": "out", "target_language": "vi"})]
