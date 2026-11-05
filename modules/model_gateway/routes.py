import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.config import Settings
from core.database import get_session
from core.model_gateway.client import (
    CapabilityUnsupported,
    ModelGateway,
    ModelGatewayError,
    PrivacyPolicyDenied,
)
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import DraftProbeRequest, ModelMapping, ProbeRequest, RequestPolicy
from core.rate_limit import require_budget
from core.workspaces.dependencies import require_workspace_read, require_workspace_write
from core.workspaces.schemas import WorkspaceContext
from modules.settings import public as ai_settings

router = APIRouter(prefix="/api/v1/settings/models", tags=["models"])
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]
OwnerRead = Annotated[WorkspaceContext, Depends(require_workspace_read)]
Session = Annotated[AsyncSession, Depends(get_session)]


_NOT_PROVED = "Gateway answered, but the response lacked the data this capability requires"


def _capability_proved(capability: str, response: object) -> bool:
    """Require capability-specific response evidence before recording support."""
    if not isinstance(response, dict):
        return False
    if capability == "embeddings":
        rows = response.get("data")
        return bool(rows) and isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("embedding"), list) and bool(row["embedding"]) for row in rows)
    if capability == "reranking":
        rows = response.get("results")
        return bool(rows) and isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("index"), int) and not isinstance(row.get("index"), bool) and isinstance(row.get("relevance_score", row.get("score")), (int, float)) and not isinstance(row.get("relevance_score", row.get("score")), bool) for row in rows)
    if capability == "streaming":
        return isinstance(response.get("streamed"), str) and bool(response["streamed"].strip())
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return False
    if capability == "tools":
        return bool(message.get("tool_calls"))
    if capability == "structured":
        try:
            return isinstance(json.loads(message.get("content", "")), dict)
        except (TypeError, json.JSONDecodeError):
            return False
    return isinstance(message.get("content"), str)



class ModelTestResultBody(BaseModel):
    """One finished per-model test outcome reported by the Models table."""

    provider_id: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    result: Literal["supported", "failed", "unsupported"]


# Capabilities that describe whether the model itself answers; tools/structured are optional features with
# fallbacks and must not mark the whole model "unsupported" in the Models table.
_PRIMARY_CAPABILITIES = frozenset({"chat", "streaming", "embeddings", "reranking"})


@router.get("/test-results")
async def list_test_results(session: Session, _owner: OwnerRead) -> list[dict[str, object]]:
    """Return one row per tested provider model: its newest primary outcome plus every per-capability result.

    Backed by the same evidence the gateway records from real chat/embedding calls, so the table shows the latest
    real outcome (supported / unsupported / failed) with ``checked_at``. ``capabilities`` maps each recorded
    capability to its result.
    """
    rows = await ai_settings.list_model_test_results(session, workspace_id=_owner.workspace_id)
    grouped: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        grouped.setdefault((r.provider_id, r.model_id), []).append(r)
    out: list[dict[str, object]] = []
    for (provider_id, model_id), items in grouped.items():
        primary = [r for r in items if r.capability in _PRIMARY_CAPABILITIES]
        if not primary:
            continue
        latest = max(primary, key=lambda r: r.checked_at)
        out.append({"provider_id": provider_id, "model": model_id, "result": latest.result,
                    "checked_at": latest.checked_at.isoformat(), "capabilities": {r.capability: r.result for r in items}})
    return out


@router.put("/test-results")
async def save_test_result(body: ModelTestResultBody, session: Session, _owner: OwnerWrite) -> dict[str, object]:
    """Persist the latest test outcome of one model so the next visit shows it with its check time."""
    row = await ai_settings.save_model_test_result(
        session, workspace_id=_owner.workspace_id, provider_id=body.provider_id, model_id=body.model, result=body.result)
    if row is None:
        raise HTTPException(status_code=500, detail="Test result could not be stored")
    out: dict[str, object] = {"provider_id": row.provider_id, "model": row.model_id, "result": row.result, "checked_at": row.checked_at.isoformat()}
    await session.commit()
    return out


async def _collect_stream_text(
    client: ModelGateway, role: str, mapping: ModelMapping, policy: RequestPolicy,
) -> str:
    """Stream a synthetic chat and return the concatenated delta text (bounded to ~16 KiB of lines).

    ``aclosing`` guarantees a ``break`` closes the async generator immediately (``stream()`` is an
    async generator at runtime, typed AsyncIterator), otherwise the model slot lease is held until
    GC. Unparseable ``data:`` lines are skipped; gateway errors propagate to the caller.
    """
    message = [{"role": "user", "content": "Reply with the word ready."}]
    text, total = "", 0
    async with aclosing(cast("AsyncGenerator[str, None]", client.stream(role, mapping, policy, message, probe=True))) as lines:
        async for line in lines:
            total += len(line)
            if line.startswith("data:"):
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                            text += delta["content"]
                except (TypeError, json.JSONDecodeError):
                    pass
            if total >= 16384:
                break
    return text


async def _run_capability_probe(
    client: ModelGateway, capability: str, role: str, mapping: ModelMapping, policy: RequestPolicy,
) -> Any:
    """Send the synthetic request for one capability through the gateway and return its raw response.

    Dispatches by ``capability`` (embeddings, reranking, streaming, structured, tools; anything else is
    a chat probe). Streaming returns ``{"streamed": text}``. Performs provider I/O; every gateway or
    policy exception propagates so the route can map it (403/503/unsupported/failed).
    """
    message = [{"role": "user", "content": "Reply with the word ready."}]
    if capability == "embeddings":
        return await client.embed(role, mapping, policy, ["Synthetic capability probe."], probe=True)
    if capability == "reranking":
        return await client.rerank(role, mapping, policy, "synthetic probe", ["synthetic probe document"], probe=True)
    if capability == "streaming":
        return {"streamed": await _collect_stream_text(client, role, mapping, policy)}
    if capability == "structured":
        return await client.structured(role, mapping, policy, message, {"name": "probe", "schema": {"type": "object"}}, probe=True)
    if capability == "tools":
        return await client.tools(role, mapping, policy, [{"role": "user", "content": "Call the probe tool now."}], [{"type": "function", "function": {"name": "probe", "description": "Return a synthetic readiness signal.", "parameters": {"type": "object", "properties": {}}}}], probe=True)
    return await client.chat(role, mapping, policy, message, probe=True)


def _resolve_probe_target(
    base_config: Any, role: str, body: ProbeRequest,
) -> tuple[Any, ModelMapping | None, ModelMapping | None]:
    """Pick the config and mapping a role probe targets; return ``(config, mapping, configured_mapping)``.

    With body ``provider_id`` + ``model`` (chat role only) the model must be an ``llm`` model listed on
    that saved provider and the probe derives its config from the composer-override mapping
    (``configured_mapping`` is that mapping); otherwise the role default is used. Raises 422 for a
    non-chat override, 404 for an unknown provider/model and 409 for a non-llm model. Pure.
    """
    if body.provider_id is not None and body.model is not None:
        if role != "chat":
            raise HTTPException(status_code=422, detail="Provider model probes support the chat role only")
        provider = base_config.providers.get(body.provider_id)
        entry = next((m for m in provider.models if m.id == body.model), None) if provider else None
        if entry is None:
            raise HTTPException(status_code=404, detail="Model is not configured on this provider")
        if entry.type != "llm":
            raise HTTPException(status_code=409, detail="Only chat (llm) models can be probed for chat")
        configured_mapping = ai_settings.mapping_for_override(base_config, body.provider_id, body.model)
        return ai_settings.derive_for_mapping(base_config, configured_mapping), configured_mapping, configured_mapping
    config = ai_settings.derive_for_role(base_config, role)
    return config, config.defaults.resolve(role), None


@router.post("/draft-test", dependencies=[Depends(require_budget("model_probe", 10, 60))])
async def probe_draft(
    request: Request, session: Session, _owner: OwnerWrite,
    body: DraftProbeRequest,
) -> dict[str, object]:
    """Probe an unsaved remote draft with synthetic input and transient authorization.

    A supported ``embeddings`` probe also returns ``dimensions``, the length of the returned vector. The optional
    body ``role`` only labels telemetry; a draft never selects or stores a saved role mapping.
    """
    role = body.role or "chat"
    settings: Settings = request.app.state.settings
    redis: Redis = request.app.state.redis
    mapping = ModelMapping(model=body.model, version=body.version, destination="remote")
    config = await ai_settings.get_ai_execution_config(session, settings, redis, scope=_owner)
    endpoint, kind = ai_settings.resolve_draft_endpoint(
        config, str(body.base_url) if body.base_url else None, body.kind, body.provider_id, settings)
    credential, local, destination = ai_settings.resolve_draft_provider(config, endpoint, body.api_key, body.provider_id, kind)
    if not credential and not local:
        raise HTTPException(status_code=409, detail="Enter the draft gateway credential to probe")
    session_ref = authenticated_session_ref(request)
    identity = hashlib.sha256(json.dumps((config.gateway_identity, endpoint, hashlib.sha256(credential.encode()).hexdigest())).encode()).hexdigest()
    await session.rollback()
    policy = RequestPolicy(
        workspace_id=_owner.workspace_id, actor_user_id=_owner.user_id, membership_revision=_owner.membership_revision,
        gateway_identity=identity,
        # A user-initiated synthetic draft probe is the explicit, transient
        # authorization; it cannot create production capability evidence.
        reasoning_allowed=True,
        embeddings_allowed=True,
        permitted_destinations=frozenset({destination} if destination else set()),
        reasoning_destinations=frozenset({destination}),
        embedding_destinations=frozenset({destination}),
        configuration_revision=0,
    )
    if body.capability in {"embeddings", "reranking"} and not policy.embeddings_allowed:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy")
    if body.capability not in {"embeddings", "reranking"} and not policy.reasoning_allowed:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy")

    async def recheck_send() -> None:
        """Fresh exact-session/access/config and draft endpoint check, rollback before provider I/O."""
        try:
            await ai_settings.check_ai_execution_config(session, settings, redis, scope=_owner,
                                                        expected=config, auth_sessions=(session_ref,))
            ai_settings.validate_endpoint(endpoint, settings)
        finally:
            await session.rollback()

    # Probe with the owner's configured request timeout: a fixed 15 s marked slow reasoning models as failed.
    client = ModelGateway(redis, endpoint, credential, destination or "remote",
        timeout_seconds=float(config.request_timeout_seconds), scope=_owner, configuration_revision=0, gateway_identity=identity, before_send=recheck_send,
        approved_endpoint_cidrs=tuple(settings.ai_allowed_endpoint_cidrs), allow_public_endpoints=settings.ai_allow_public_providers, kind=kind)
    detail = ""  # real failure reason for the owner; empty when supported
    try:
        response = await _run_capability_probe(client, body.capability, role, mapping, policy)
        result = "supported" if _capability_proved(body.capability, response) else "unsupported"
        if result == "unsupported":
            detail = _NOT_PROVED
    except PrivacyPolicyDenied as exc:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy") from exc
    except CapabilityUnsupported as exc:
        detail, result = str(exc), "unsupported"
    except (ModelGatewayError, ValueError) as exc:  # unparseable gateway reply: truthful failure, not a 500
        detail, result = str(exc), "failed"
    await recheck_send()
    out: dict[str, object] = {"role": role, "model": mapping.model, "version": mapping.version,
                              "gateway_identity": identity, "configuration_revision": 0,
                              "capability": body.capability, "result": result, "detail": detail}
    if body.capability == "embeddings" and result == "supported":
        out["dimensions"] = len(response["data"][0]["embedding"])  # _capability_proved guarantees this shape
    return out


@router.post("/{role}/test", dependencies=[Depends(require_budget("model_probe", 10, 60))])
async def probe_model(
    request: Request, session: Session, _owner: OwnerWrite,
    role: Annotated[str, Path(pattern=r"^(chat|brief|background|embedding|reranker|vision)$")],
    body: ProbeRequest,
) -> dict[str, object]:
    """Probe a configured role default, or a saved provider's model, and persist its capability result.

    With body ``provider_id`` + ``model`` (chat role only) the model must be an ``llm`` model listed on that
    saved provider; the probe then targets that provider's endpoint and stores evidence under the same mapping
    the chat worker builds for a composer override. Authorized by the owner write dependency; evidence is
    saved only after the real provider call and the post-I/O configuration recheck.
    Raises 409 when the role is unconfigured, 404 for an unknown provider/model, 422 for a non-chat override.
    """
    settings: Settings = request.app.state.settings
    redis: Redis = request.app.state.redis
    base_config = await ai_settings.get_ai_execution_config(session, settings, redis, scope=_owner)
    config, mapping, configured_mapping = _resolve_probe_target(base_config, role, body)
    if mapping is None:
        raise HTTPException(status_code=409, detail="Configure this model role first")
    destination = config.endpoint_destination_id
    policy = RequestPolicy(
        workspace_id=config.workspace_id, actor_user_id=config.actor_user_id, membership_revision=config.membership_revision,
        gateway_identity=config.gateway_identity,
        reasoning_allowed=config.privacy.allow_remote_reasoning,
        embeddings_allowed=config.privacy.allow_remote_embeddings,
        permitted_destinations=frozenset({destination} if destination else set()),
        reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
        embedding_destinations=frozenset(config.privacy.embedding_destinations),
        configuration_revision=config.configuration_revision,
    )
    local = (destination or "").startswith("local:")  # local providers need no remote consent
    if body.capability in {"embeddings", "reranking"} and not policy.embeddings_allowed and not local:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy")
    if body.capability not in {"embeddings", "reranking"} and not policy.reasoning_allowed and not local:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy")
    async def recheck_send() -> None:
        """Recheck exact session, workspace and complete privacy/config identity each attempt.

        Any revision/configuration change rejects this probe; every path rolls back before
        provider network. No lock is released by after_send or retained across HTTP.
        """
        try:
            await ai_settings.check_ai_execution_config(session, settings, redis, scope=_owner,
                                                        expected=config, auth_sessions=(session_ref,))
            if not may_send(policy, role, mapping, destination or "remote", bool(config.gateway_api_key), body.capability):
                raise PrivacyPolicyDenied("Probe denied by current settings")
        finally:
            await session.rollback()

    session_ref = authenticated_session_ref(request)
    await session.rollback()
    # Probe with the owner's configured request timeout: a fixed 15 s marked slow reasoning models as failed.
    client = ModelGateway(redis, config.gateway_base_url, config.gateway_api_key,
        destination or "remote", timeout_seconds=float(config.request_timeout_seconds), scope=_owner,
        configuration_revision=config.configuration_revision, gateway_identity=config.gateway_identity, model_identity=config.model_identity,
        before_send=recheck_send, approved_endpoint_cidrs=config.endpoint_allowed_cidrs,
        allow_public_endpoints=config.allow_public_providers, kind=config.gateway_kind)
    detail = ""  # real failure reason for the owner; empty when supported
    try:
        response = await _run_capability_probe(client, body.capability, role, mapping, policy)
        proved = _capability_proved(body.capability, response)
        if not proved:
            detail = _NOT_PROVED
        result = ai_settings.new_capability_result(role, mapping, body.capability, "supported" if proved else "unsupported", config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except PrivacyPolicyDenied as exc:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy") from exc
    except CapabilityUnsupported as exc:
        detail = str(exc)
        result = ai_settings.new_capability_result(role, mapping, body.capability, "unsupported", config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except (ModelGatewayError, ValueError) as exc:  # unparseable gateway reply: failed, not a 500
        detail = str(exc)
        result = ai_settings.new_capability_result(role, mapping, body.capability, "failed", config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except RedisError as exc:  # breaker/lease/quota state; capability evidence itself is in Postgres
        raise HTTPException(status_code=503, detail="Model gateway state is unavailable") from exc
    try:
        await ai_settings.check_ai_execution_config(session, settings, redis, scope=_owner,
                                                    expected=config, auth_sessions=(session_ref,))
        await ai_settings.save_capability(session, result, config=config, scope=_owner,
                                          multi_workspace_enabled=settings.multi_workspace_enabled, configured_mapping=configured_mapping)
        await session.commit()
    except ValueError as exc:
        # Evidence no longer matches the configured model (changed during the test): a clear 409, never a 500.
        raise HTTPException(status_code=409, detail="Model configuration changed during the test; test again") from exc
    finally:
        await session.rollback()
    return {**result.model_dump(mode="json"), "detail": detail}
