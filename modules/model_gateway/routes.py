import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.public import authenticated_session_ref
from core.workspaces.dependencies import require_workspace_write
from core.workspaces.schemas import WorkspaceContext
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
from modules.settings import public as ai_settings

router = APIRouter(prefix="/api/v1/settings/models", tags=["models"])
OwnerWrite = Annotated[WorkspaceContext, Depends(require_workspace_write)]
Session = Annotated[AsyncSession, Depends(get_session)]


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



@router.post("/{alias}/draft-test")
async def probe_draft(
    request: Request, session: Session, _owner: OwnerWrite,
    alias: Annotated[str, Path(pattern=r"^(reasoning-large|reasoning-small|fast|embedding|reranker|vision|local-private)$")],
    body: DraftProbeRequest,
) -> dict[str, object]:
    """Probe an unsaved remote draft with synthetic input and transient authorization."""
    settings: Settings = request.app.state.settings
    redis: Redis = request.app.state.redis
    endpoint = ai_settings.validate_endpoint(str(body.base_url), settings)
    if endpoint is None:
        raise HTTPException(status_code=422, detail="Gateway endpoint is required")
    mapping = ModelMapping(model=body.model, version=body.version, destination="remote")
    credential = body.api_key
    if not credential:
        raise HTTPException(status_code=409, detail="Enter the draft gateway credential to probe")
    destination = f"omniroute:{hashlib.sha256(endpoint.encode()).hexdigest()[:32]}"
    config = await ai_settings.get_ai_execution_config(session, settings, redis, scope=_owner)
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

    client = ModelGateway(redis, endpoint, credential, destination or "omniroute",
        timeout_seconds=15, scope=_owner, configuration_revision=0, gateway_identity=identity, before_send=recheck_send,
        approved_endpoint_cidrs=tuple(settings.ai_allowed_endpoint_cidrs))
    message = [{"role": "user", "content": "Reply with the word ready."}]
    try:
        if body.capability == "embeddings":
            response = await client.embed(alias, mapping, policy, ["Synthetic capability probe."], probe=True)
        elif body.capability == "reranking":
            response = await client.rerank(alias, mapping, policy, "synthetic probe", ["synthetic probe document"], probe=True)
        elif body.capability == "streaming":
            chunks, total = "", 0
            async for line in client.stream(alias, mapping, policy, message, probe=True):
                total += len(line)
                if line.startswith("data:"):
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        item = json.loads(data)
                        choices = item.get("choices", []) if isinstance(item, dict) else []
                        if choices and isinstance(choices[0], dict):
                            delta = choices[0].get("delta", {})
                            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                                chunks += delta["content"]
                    except (TypeError, json.JSONDecodeError):
                        pass
                if total >= 16384:
                    break
            response = {"streamed": chunks}
        elif body.capability == "structured":
            response = await client.structured(alias, mapping, policy, message, {"name": "probe", "schema": {"type": "object"}}, probe=True)
        elif body.capability == "tools":
            response = await client.tools(alias, mapping, policy, [{"role": "user", "content": "Call the probe tool now."}], [{"type": "function", "function": {"name": "probe", "description": "Return a synthetic readiness signal.", "parameters": {"type": "object", "properties": {}}}}], probe=True)
        else:
            response = await client.chat(alias, mapping, policy, message, probe=True)
        result = "supported" if _capability_proved(body.capability, response) else "unsupported"
    except PrivacyPolicyDenied as exc:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy") from exc
    except CapabilityUnsupported:
        result = "unsupported"
    except (ModelGatewayError, ValueError):  # unparseable gateway reply: truthful failure, not a 500
        result = "failed"
    await recheck_send()
    return {"alias": alias, "model": mapping.model, "version": mapping.version,
            "gateway_identity": identity, "configuration_revision": 0,
            "capability": body.capability, "result": result}


@router.post("/{alias}/test")
async def probe_model(
    request: Request, session: Session, _owner: OwnerWrite,
    alias: Annotated[str, Path(pattern=r"^(reasoning-large|reasoning-small|fast|embedding|reranker|vision|local-private)$")],
    body: ProbeRequest,
) -> dict[str, object]:
    """Probe a configured alias under current privacy policy and persist its capability result."""
    settings: Settings = request.app.state.settings
    redis: Redis = request.app.state.redis
    config = await ai_settings.get_ai_execution_config(session, settings, redis, scope=_owner)
    mapping = config.aliases.get(alias)
    if mapping is None:
        raise HTTPException(status_code=409, detail="Configure this model alias first")
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
    if body.capability in {"embeddings", "reranking"} and not policy.embeddings_allowed:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy")
    if body.capability not in {"embeddings", "reranking"} and not policy.reasoning_allowed:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy")
    async def recheck_send() -> None:
        """Recheck exact session, workspace and complete privacy/config identity each attempt.

        Any revision/configuration change rejects this probe; every path rolls back before
        provider network. No lock is released by after_send or retained across HTTP.
        """
        try:
            await ai_settings.check_ai_execution_config(session, settings, redis, scope=_owner,
                                                        expected=config, auth_sessions=(session_ref,))
            if not may_send(policy, alias, mapping, destination or "omniroute", bool(config.omniroute_api_key), body.capability):
                raise PrivacyPolicyDenied("Probe denied by current settings")
        finally:
            await session.rollback()

    session_ref = authenticated_session_ref(request)
    await session.rollback()
    client = ModelGateway(redis, config.omniroute_base_url, config.omniroute_api_key,
        destination or "omniroute", timeout_seconds=15, scope=_owner,
        configuration_revision=config.configuration_revision, gateway_identity=config.gateway_identity,
        before_send=recheck_send, approved_endpoint_cidrs=config.endpoint_allowed_cidrs)
    message = [{"role": "user", "content": "Reply with the word ready."}]
    try:
        if body.capability == "embeddings":
            response = await client.embed(alias, mapping, policy, ["Synthetic capability probe."], probe=True)
        elif body.capability == "reranking":
            response = await client.rerank(alias, mapping, policy, "synthetic probe", ["synthetic probe document"], probe=True)
        elif body.capability == "streaming":
            text, total = "", 0
            async for line in client.stream(alias, mapping, policy, message, probe=True):
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
            response = {"streamed": text}
        elif body.capability == "structured":
            response = await client.structured(alias, mapping, policy, message, {"name": "probe", "schema": {"type": "object"}}, probe=True)
        elif body.capability == "tools":
            response = await client.tools(alias, mapping, policy, [{"role": "user", "content": "Call the probe tool now."}], [{"type": "function", "function": {"name": "probe", "description": "Return a synthetic readiness signal.", "parameters": {"type": "object", "properties": {}}}}], probe=True)
        else:
            response = await client.chat(alias, mapping, policy, message, probe=True)
        result = ai_settings.new_capability_result(alias, mapping, body.capability, "supported" if _capability_proved(body.capability, response) else "unsupported", config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except PrivacyPolicyDenied as exc:
        raise HTTPException(status_code=403, detail="Probe denied by privacy policy") from exc
    except CapabilityUnsupported:
        result = ai_settings.new_capability_result(alias, mapping, body.capability, "unsupported", config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except (ModelGatewayError, ValueError):  # unparseable gateway reply: failed, not a 500
        result = ai_settings.new_capability_result(alias, mapping, body.capability, "failed", config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Model capability storage is unavailable") from exc
    try:
        await ai_settings.check_ai_execution_config(session, settings, redis, scope=_owner,
                                                    expected=config, auth_sessions=(session_ref,))
        await ai_settings.save_capability(redis, result, config=config, scope=_owner, multi_workspace_enabled=settings.multi_workspace_enabled)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Model capability storage is unavailable") from exc
    finally:
        await session.rollback()
    return result.model_dump(mode="json")
