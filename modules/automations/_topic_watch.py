"""Topic watch lifecycle for automation rules: Source provisioning, one-time consent and retirement.

A Topic watch rule holds one ``collect_source`` action. Saving the rule provisions (or updates) a
task-owned Source through the connectors owner; the owner's consent is stored as a hash of the
consent-bound definition and is valid only while that hash still matches. Only the HTTP routes may
grant consent (``allow_consent=True``), so an agent or tool path can never grant it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ForbiddenError, ValidationFailedError
from core.workspaces.schemas import Scope, WorkspaceContext
from modules.automations.schemas import CollectSourceAction, topic_consent_hash
from modules.connectors import public as connectors

logger = logging.getLogger(__name__)


def _writer(scope: Scope) -> WorkspaceContext:
    """Return ``scope`` as an interactive workspace context; Source writes never run under job or admin scopes."""
    if not isinstance(scope, WorkspaceContext):
        raise ForbiddenError(
            "Topic watch Sources are managed from an interactive workspace session"
        )
    return scope


def previous_watch(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the stored ``collect_source`` action of a revision, or None when it holds none."""
    return next((a for a in actions if a.get("type") == "collect_source"), None)


def _consent(
    spec: CollectSourceAction,
    new_hash: str,
    previous: dict[str, Any] | None,
    *,
    estimate: int,
    allow_consent: bool,
) -> dict[str, Any] | None:
    """Decide the consent to persist: a fresh grant, the carried previous grant, or None.

    A grant is honoured only when ``allow_consent`` is true and the owner's accepted estimate equals the
    server estimate (``ValidationFailedError("consent_estimate_mismatch")`` otherwise). Without a grant,
    the previous consent survives only while its hash equals the new definition's hash, so editing the
    topic, schedule, sites, item cap or model role clears it. Client-sent ``consent`` is never used.
    """
    if spec.grant_consent_est_tokens is not None and allow_consent:
        if spec.grant_consent_est_tokens != estimate:
            raise ValidationFailedError("consent_estimate_mismatch")
        return {
            "hash": new_hash,
            "granted_at": datetime.now(UTC).isoformat(),
            "est_tokens": estimate,
        }
    stored = (previous or {}).get("consent")
    if isinstance(stored, dict) and stored.get("hash") == new_hash:
        return stored
    return None


async def prepare_topic_watch(
    session: AsyncSession,
    action: dict[str, Any],
    trigger: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    name: str,
    scope: Scope,
    multi_workspace_enabled: bool,
    allow_consent: bool,
) -> tuple[dict[str, Any], bool]:
    """Return the ``collect_source`` action to persist and whether a new Source was provisioned.

    Resolves consent first (a mismatched estimate raises before any Source exists), then provisions the
    Source on first save or re-configures it when the watch spec or model role changed. The returned
    dict carries the server-assigned ``source_id`` and ``consent`` and never the request-only
    ``grant_consent_est_tokens``. Source creation commits inside the sources owner; the caller must
    compensate with ``retire_topic_watch`` if a later step fails for a freshly provisioned Source.
    Raises ``ValidationFailedError`` or the connectors owner's ``ConflictError``/``NotFoundError``.
    """
    spec = CollectSourceAction.model_validate(action)
    watch = spec.watch.model_dump(mode="json")
    estimate = connectors.estimate_tokens(spec.watch.max_items)
    new_hash = topic_consent_hash(watch, trigger, spec.model_role)
    consent = _consent(spec, new_hash, previous, estimate=estimate, allow_consent=allow_consent)
    config = connectors.TopicWatchConfig(
        topic=spec.watch.topic,
        keep_rule=spec.watch.keep_rule,
        languages=tuple(spec.watch.languages),
        include_sites=tuple(spec.watch.include_sites),
        exclude_sites=tuple(spec.watch.exclude_sites),
        max_items=spec.watch.max_items,
        token_budget=connectors.token_cap(estimate),
        model_role=spec.model_role,
    )
    source_id = (previous or {}).get("source_id")
    created = source_id is None
    if created:
        source_id = await connectors.provision_topic_watch_source(
            session,
            name=name,
            config=config,
            scope=_writer(scope),
            multi_workspace_enabled=multi_workspace_enabled,
        )
    elif (previous or {}).get("watch") != watch or (previous or {}).get(
        "model_role"
    ) != spec.model_role:
        await connectors.update_topic_watch_source(
            session,
            UUID(str(source_id)),
            config,
            scope=_writer(scope),
            multi_workspace_enabled=multi_workspace_enabled,
        )
    stored: dict[str, Any] = {
        "type": "collect_source",
        "watch": watch,
        "model_role": spec.model_role,
        "source_id": str(source_id),
    }
    if consent is not None:
        stored["consent"] = consent
    return stored, created


async def prepare_topic_actions(
    session: AsyncSession,
    actions: list[dict[str, Any]],
    trigger: dict[str, Any],
    previous: list[dict[str, Any]],
    *,
    name: str,
    scope: Scope,
    multi_workspace_enabled: bool,
    allow_consent: bool,
) -> tuple[list[dict[str, Any]], list[UUID]]:
    """Replace each dumped ``collect_source`` action with its prepared form; return the new Sources.

    The second item lists Sources provisioned by this call, which the caller retires if the rule's own
    write later fails (no orphan Source). Other actions pass through untouched.
    """
    before = previous_watch(previous)
    prepared: list[dict[str, Any]] = []
    fresh: list[UUID] = []
    for action in actions:
        if action.get("type") != "collect_source":
            prepared.append(action)
            continue
        try:
            stored, created = await prepare_topic_watch(
                session,
                action,
                trigger,
                before,
                name=name,
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
                allow_consent=allow_consent,
            )
        except BaseException:
            await retire_sources(
                session, fresh, scope=scope, multi_workspace_enabled=multi_workspace_enabled
            )
            raise
        prepared.append(stored)
        if created:
            fresh.append(UUID(stored["source_id"]))
    return prepared, fresh


async def retire_sources(
    session: AsyncSession,
    source_ids: list[UUID],
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Archive Sources provisioned for a rule whose write failed; best effort, never masks the cause.

    Rolls the session back first so the owner commands start from a clean transaction. Errors are
    logged by type only; a Source that cannot be archived stays archivable from the Sources page.
    """
    if not source_ids:
        return
    await session.rollback()
    for source_id in source_ids:
        try:
            await connectors.retire_topic_watch_source(
                session,
                source_id,
                purge_items=False,
                scope=_writer(scope),
                multi_workspace_enabled=multi_workspace_enabled,
            )
        except Exception as exc:  # noqa: BLE001  # compensation must not mask the original failure
            logger.warning("Topic watch compensation failed; error_type=%s", type(exc).__name__)


async def retire_topic_watch(
    session: AsyncSession,
    action: dict[str, Any],
    *,
    purge_items: bool,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Retire the Source of a stored ``collect_source`` action: archive it, or purge its items.

    ``purge_items=True`` uses the sources purge operation. The owner commands commit their own work,
    so call this after the rule's own transaction has been committed. A missing Source is a no-op.
    """
    source_id = action.get("source_id")
    if not source_id:
        return
    await connectors.retire_topic_watch_source(
        session,
        UUID(str(source_id)),
        purge_items=purge_items,
        scope=_writer(scope),
        multi_workspace_enabled=multi_workspace_enabled,
    )
