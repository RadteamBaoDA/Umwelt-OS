"""Translation settings and batch admission/read services (derived data only).

Authorization is deny-by-default: a resource type is admitted only after its owner module
registers an authorizer (W3 News/Brief projections). Callers commit; no network I/O happens here.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.schemas import AccountSessionRef
from core.workspaces.public import lock_access_fence, read_access_fence
from core.workspaces.schemas import Scope, WorkspaceContext
from modules.settings.models import TranslationSettingsRecord
from modules.settings.schemas import TranslationSettingsRead, TranslationSettingsUpdate
from modules.translations.models import (
    ContentTranslation,
    TranslationBatch,
    TranslationBatchItem,
)
from modules.translations.schemas import (
    TranslationBatchAccepted,
    TranslationBatchRead,
    TranslationBatchRequest,
    TranslationItemRead,
    TranslationItemStatusRead,
    TranslationPayload,
)

PROMPT_VERSION = "v2"  # T2 owns prompt text; bumping it invalidates every cache fingerprint.
MAX_NONTERMINAL_PER_WORKSPACE = 100
MAX_BATCHES_PER_MINUTE = 4
RETRY_AFTER_SECONDS = "60"


@dataclass(frozen=True, slots=True)
class ResourceAuthorization:
    """Owner-module verdict for one currently visible resource; hashes feed the cache fingerprint."""

    resource_revision: str
    content_hash: str
    visibility_hash: str


ResourceAuthorizer = Callable[..., Awaitable[ResourceAuthorization | None]]
_AUTHORIZERS: dict[str, ResourceAuthorizer] = {}


def register_resource_authorizer(resource_type: str, authorizer: ResourceAuthorizer) -> None:
    """Register ``authorizer(session, *, scope, resource_id, multi_workspace_enabled)``; None unless shared-readable."""
    _AUTHORIZERS[resource_type] = authorizer


async def _authorize(
    session: AsyncSession, scope: WorkspaceContext, resource_type: str, resource_id: UUID,
    *, multi_workspace_enabled: bool,
) -> ResourceAuthorization | None:
    """Deny by default when no owner module registered for the type."""
    authorizer = _AUTHORIZERS.get(resource_type)
    if authorizer is None:
        return None
    return await authorizer(
        session, scope=scope, resource_id=resource_id, multi_workspace_enabled=multi_workspace_enabled,
    )


def _member(scope: Scope) -> WorkspaceContext:
    """Translation requests are interactive member/owner actions only."""
    if not isinstance(scope, WorkspaceContext):
        raise HTTPException(status_code=401, detail="Authentication required")
    return scope


def _read(row: TranslationSettingsRecord | None) -> TranslationSettingsRead:
    """Project a row (or the default for an absent row) to the public DTO."""
    if row is None:
        return TranslationSettingsRead()
    return TranslationSettingsRead(
        enabled=row.enabled, target_language=row.target_language,
        configuration_revision=row.configuration_revision,
    )


async def read_translation_settings(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> TranslationSettingsRead:
    """Member-safe read projection; admission precedes the workspace-predicated query."""
    await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    row = await session.scalar(select(TranslationSettingsRecord).where(
        TranslationSettingsRecord.workspace_id == scope.workspace_id,
    ).execution_options(populate_existing=True))
    return _read(row)


async def save_translation_settings(
    session: AsyncSession, value: TranslationSettingsUpdate,
    *, scope: Scope, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> TranslationSettingsRead:
    """Owner CAS save under the locked access fence; caller commits.

    A change bumps the revision (making every ready cache ineligible) and blocks pending work.
    """
    owner = _member(scope)
    if owner.role != "owner":
        raise HTTPException(status_code=403, detail="Workspace owner required")
    await lock_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, auth_sessions=auth_sessions,
    )
    await session.execute(insert(TranslationSettingsRecord).values(workspace_id=scope.workspace_id)
                          .on_conflict_do_nothing(index_elements=["workspace_id"]))
    row = await session.scalar(select(TranslationSettingsRecord).where(
        TranslationSettingsRecord.workspace_id == scope.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise RuntimeError("Translation settings could not be initialized")
    if row.configuration_revision != value.expected_revision:
        raise HTTPException(status_code=409, detail="Translation settings changed; reload before saving")
    if row.enabled == value.enabled and row.target_language == value.target_language:
        return _read(row)
    row.enabled = value.enabled
    row.target_language = value.target_language
    row.configuration_revision += 1
    row.updated_by_user_id = owner.user_id
    await session.execute(update(ContentTranslation).where(
        ContentTranslation.workspace_id == scope.workspace_id, ContentTranslation.status == "pending",
    ).values(
        status="blocked", error_code="translation_disabled" if not value.enabled else "settings_changed",
        lease_token=None, lease_expires_at=None, slot_token=None, slot_expires_at=None,
    ))
    await session.flush()
    return _read(row)


def translation_fingerprint(parts: dict[str, str | int]) -> str:
    """Stable sha256 over every cache-relevant part (key order independent)."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def config_hash_for(
    *, workspace_id: UUID, actor_user_id: int, resource_revision: str, content_hash: str, visibility_hash: str,
    target_language: str, settings_revision: int, policy_fingerprint: str,
) -> str:
    """The one cache fingerprint; the worker recomputes it before every send and at publish."""
    return translation_fingerprint({
        "workspace": str(workspace_id), "actor": actor_user_id, "revision": resource_revision,
        "content": content_hash, "visibility": visibility_hash, "target": target_language,
        "settings": settings_revision, "policy": policy_fingerprint, "prompt": PROMPT_VERSION,
    })


async def policy_fingerprint(
    session: AsyncSession, app_settings: object | None, redis: object | None, workspace_id: UUID,
) -> str:
    """AI policy identity for the fingerprint (public for the worker)."""
    return await _policy_fingerprint(session, app_settings, redis, workspace_id)


async def _policy_fingerprint(
    session: AsyncSession, app_settings: object | None, redis: object | None, workspace_id: UUID,
) -> str:
    """AI policy identity (W4-ai); empty until that hook exists or settings are not supplied."""
    reader = getattr(import_module("modules.settings.public"), "read_ai_policy_fingerprint", None)
    if reader is None or app_settings is None:
        return ""
    return (await reader(session, app_settings, redis, workspace_id=workspace_id, alias="reasoning-small")) or "none"


def _too_many(detail: str) -> HTTPException:
    """429 with the contractual 60 second retry."""
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": RETRY_AFTER_SECONDS})


class TranslationBatchEnqueue(TranslationBatchAccepted):
    """Accepted body plus the fresh row ids to enqueue after commit (never serialized)."""

    enqueue_ids: list[UUID] = Field(default_factory=list, exclude=True)


async def submit_batch(
    session: AsyncSession, request: TranslationBatchRequest,
    *, scope: Scope, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
    app_settings: object | None = None, redis: object | None = None,
) -> TranslationBatchAccepted:
    """Admit a batch; see ``request_translations`` for the enqueue-aware variant."""
    return await request_translations(
        session, _member(scope), request, multi_workspace_enabled=multi_workspace_enabled,
        auth_sessions=auth_sessions, app_settings=app_settings, redis=redis,
    )


async def request_translations(
    session: AsyncSession, scope: WorkspaceContext, request: TranslationBatchRequest,
    *, multi_workspace_enabled: bool, auth_sessions: tuple[AccountSessionRef, ...] = (),
    app_settings: object | None = None, redis: object | None = None,
) -> TranslationBatchEnqueue:
    """Authorize every item first (404 uniformly, then 409 stale), dedup, bound, persist pending rows.

    The caller commits, then enqueues ``enqueue_ids`` best effort (recovery covers a lost enqueue).
    A ``failed`` row with an unchanged fingerprint is NOT retried; it waits for new input/settings.
    """
    actor = _member(scope)
    await lock_access_fence(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, auth_sessions=auth_sessions,
    )
    row = await session.scalar(select(TranslationSettingsRecord).where(
        TranslationSettingsRecord.workspace_id == actor.workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    settings = _read(row)
    if not settings.enabled:
        return TranslationBatchEnqueue(batch_id=None, settings=settings, items=[
            TranslationItemStatusRead(resource_type=i.resource_type, resource_id=i.resource_id, status="blocked")
            for i in request.items])
    verdicts: list[ResourceAuthorization] = []
    for item in request.items:
        verdict = await _authorize(
            session, actor, item.resource_type, item.resource_id, multi_workspace_enabled=multi_workspace_enabled,
        )
        if verdict is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        verdicts.append(verdict)
    if any(v.resource_revision != i.resource_revision for v, i in zip(verdicts, request.items)):
        raise HTTPException(status_code=409, detail="Resource revision changed")
    now = datetime.now(UTC)
    policy_fp = await _policy_fingerprint(session, app_settings, redis, actor.workspace_id)
    configs = [config_hash_for(
        workspace_id=actor.workspace_id, actor_user_id=actor.user_id, resource_revision=v.resource_revision,
        content_hash=v.content_hash, visibility_hash=v.visibility_hash, target_language=settings.target_language,
        settings_revision=settings.configuration_revision, policy_fingerprint=policy_fp,
    ) for v in verdicts]
    existing = {
        (t.resource_type, t.resource_id, t.resource_revision, t.content_hash, t.visibility_hash, t.config_hash): t
        for t in (await session.scalars(select(ContentTranslation).where(
            ContentTranslation.workspace_id == actor.workspace_id,
            ContentTranslation.actor_user_id == actor.user_id,
            ContentTranslation.target_language == settings.target_language,
            ContentTranslation.config_hash.in_(configs),
            ContentTranslation.prompt_version == PROMPT_VERSION,
            ContentTranslation.resource_id.in_([i.resource_id for i in request.items]),
        ).with_for_update())).all()
    }
    rows: list[ContentTranslation] = []
    fresh: list[ContentTranslation] = []
    retry: list[ContentTranslation] = []
    for item, verdict, config in zip(request.items, verdicts, configs):
        found = existing.get((item.resource_type, item.resource_id, verdict.resource_revision,
                              verdict.content_hash, verdict.visibility_hash, config))
        if found is None:
            found = ContentTranslation(
                workspace_id=actor.workspace_id, actor_user_id=actor.user_id, resource_type=item.resource_type,
                resource_id=item.resource_id, resource_revision=verdict.resource_revision,
                content_hash=verdict.content_hash, visibility_hash=verdict.visibility_hash,
                target_language=settings.target_language, config_hash=config, prompt_version=PROMPT_VERSION,
                status="pending", attempt_count=0)
            fresh.append(found)
        elif found.expires_at <= now:
            retry.append(found)
        rows.append(found)
    if fresh or retry:  # only new work counts against the limits
        recent = await session.scalar(select(func.count()).select_from(TranslationBatch).where(
            TranslationBatch.workspace_id == actor.workspace_id,
            TranslationBatch.actor_user_id == actor.user_id,
            TranslationBatch.created_at > now - timedelta(minutes=1)))
        if (recent or 0) >= MAX_BATCHES_PER_MINUTE:
            raise _too_many("Too many translation batches")
        pending = await session.scalar(select(func.count()).select_from(ContentTranslation).where(
            ContentTranslation.workspace_id == actor.workspace_id, ContentTranslation.status == "pending"))
        if (pending or 0) + len(fresh) + len(retry) > MAX_NONTERMINAL_PER_WORKSPACE:
            raise _too_many("Translation queue is full")
    for found in retry:
        found.status, found.error_code, found.result = "pending", None, None
        found.attempt_count, found.completed_at, found.next_attempt_at = 0, None, None
        found.lease_token = found.lease_expires_at = None
        found.expires_at = now + timedelta(days=30)
    session.add_all(fresh)
    batch = TranslationBatch(
        workspace_id=actor.workspace_id, actor_user_id=actor.user_id,
        target_language=settings.target_language, settings_revision=settings.configuration_revision)
    session.add(batch)
    await session.flush()
    session.add_all(TranslationBatchItem(
        batch_id=batch.id, position=n, workspace_id=actor.workspace_id, resource_type=i.resource_type,
        resource_id=i.resource_id, resource_revision=i.resource_revision, translation_id=t.id)
        for n, (i, t) in enumerate(zip(request.items, rows)))
    await session.flush()
    return TranslationBatchEnqueue(batch_id=batch.id, settings=settings, items=[
        TranslationItemStatusRead(
            resource_type=i.resource_type, resource_id=i.resource_id, status=t.status)
        for i, t in zip(request.items, rows)], enqueue_ids=[t.id for t in (*fresh, *retry)])


async def read_batch(
    session: AsyncSession, batch_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> TranslationBatchRead:
    """Actor- and workspace-bound read; every item is reauthorized, stale or revoked items are blocked."""
    actor = _member(scope)
    await read_access_fence(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    batch = await session.scalar(select(TranslationBatch).where(
        TranslationBatch.id == batch_id, TranslationBatch.workspace_id == actor.workspace_id,
        TranslationBatch.actor_user_id == actor.user_id).execution_options(populate_existing=True))
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    settings_row = await session.scalar(select(TranslationSettingsRecord).where(
        TranslationSettingsRecord.workspace_id == actor.workspace_id).execution_options(populate_existing=True))
    settings = _read(settings_row)
    gate: str | None = None
    if not settings.enabled:
        gate = "translation_disabled"
    elif settings.configuration_revision != batch.settings_revision:
        gate = "settings_changed"
    pairs = (await session.execute(
        select(TranslationBatchItem, ContentTranslation)
        .outerjoin(ContentTranslation, (ContentTranslation.id == TranslationBatchItem.translation_id)
                   & (ContentTranslation.workspace_id == TranslationBatchItem.workspace_id)
                   & (ContentTranslation.actor_user_id == actor.user_id))
        .where(TranslationBatchItem.batch_id == batch.id, TranslationBatchItem.workspace_id == actor.workspace_id)
        .order_by(TranslationBatchItem.position).limit(25).execution_options(populate_existing=True))).all()
    now = datetime.now(UTC)
    items: list[TranslationItemRead] = []
    for item, tr in pairs:
        status, code, payload = "blocked", gate, None
        if code is None:
            verdict = await _authorize(
                session, actor, item.resource_type, item.resource_id,
                multi_workspace_enabled=multi_workspace_enabled,
            )
            if verdict is None:
                code = "resource_unavailable"
            elif tr is None:
                code = "unavailable"
            elif (verdict.resource_revision, verdict.content_hash, verdict.visibility_hash) != (
                    tr.resource_revision, tr.content_hash, tr.visibility_hash):
                code = "stale_revision"
            elif tr.expires_at <= now:
                code = "expired"
            else:
                status, code = tr.status, tr.error_code
                if status == "ready" and tr.result is not None:
                    payload = TranslationPayload.model_validate(tr.result)
        items.append(TranslationItemRead(
            resource_type=item.resource_type, resource_id=item.resource_id,
            status=status, translation=payload, target_language=batch.target_language,
            original_revision=item.resource_revision, error_code=code))
    return TranslationBatchRead(
        batch_id=batch.id, target_language=batch.target_language, items=items)
