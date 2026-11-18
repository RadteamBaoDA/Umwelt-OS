"""Owner commands that provision, update and retire the Source behind a Topic watch task.

Topic watch is a first-party native provider (``provider="topic_watch"``, source type ``web``)
collected only from its automation rule: provisioning saves the connector schedule disabled so
the connector scheduler never runs it on its own. Other modules call these commands through
``modules.connectors.public``.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError, NotFoundError
from core.workspaces.schemas import WorkspaceContext
from modules.connectors import provisioning, registry, scheduler
from modules.connectors.activation import activate_native_in_uow
from modules.connectors.public import ConnectorConfig, serialize_source_configuration
from modules.connectors.service.config import save_connector_configuration
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceCreate

# Never self-scheduled; the value only fills the (disabled) ConnectorSchedule row.
_DISABLED_INTERVAL_MINUTES = 1440


@dataclass(frozen=True)
class TopicWatchConfig:
    """Validated mirror of the ``topic_watch_*`` ConnectorConfig fields."""

    topic: str
    keep_rule: str | None
    languages: tuple[str, ...]
    include_sites: tuple[str, ...]
    exclude_sites: tuple[str, ...]
    max_items: int
    token_budget: int
    model_role: str


def _connector_config(config: TopicWatchConfig) -> ConnectorConfig:
    """Build the ConnectorConfig for a watch; empty optional lists are omitted (stored compactly).

    Raises ``pydantic.ValidationError`` (a ``ValueError``) when a field breaks its bounds.
    """
    optional: dict[str, Any] = {
        "topic_watch_keep_rule": config.keep_rule or None,
        "topic_watch_languages": list(config.languages) or None,
        "topic_watch_include_sites": list(config.include_sites) or None,
        "topic_watch_exclude_sites": list(config.exclude_sites) or None,
    }
    return ConnectorConfig(
        timeout_seconds=30,
        history_mode="returned_snapshot",
        topic_watch_topic=config.topic,
        topic_watch_max_items=config.max_items,
        topic_watch_token_budget=config.token_budget,
        topic_watch_model_role=config.model_role,
        **{key: value for key, value in optional.items() if value is not None},
    )


async def _save_and_activate(
    session: AsyncSession,
    source: ConnectorSource,
    config: TopicWatchConfig,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> None:
    """Save the watch configuration, activate the native backend and keep the schedule disabled.

    Order: validate, save source and desired settings (revision-fenced), lock the connector
    rows, activate, then upsert the schedule with ``enabled=False`` (activation enables it).
    Raises ``ConflictError`` when the revision moved or activation returns an owner-action code.
    Flush-only here; the caller commits.
    """
    try:
        source_configuration = serialize_source_configuration(source, _connector_config(config))
        registry.validate(source.model_copy(update={"configuration": source_configuration}))
    except ValueError as exc:
        raise ConflictError("topic_watch_configuration_invalid") from exc
    row = await provisioning.activation_status(
        session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    saved = await save_connector_configuration(
        session,
        source,
        row.desired_revision if row is not None else 0,
        source_configuration,
        {**source_configuration, "auth_method": "none"},
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if saved is None:
        raise ConflictError("topic_watch_configuration_conflict")
    saved[1].state = "saved_not_active"
    source_fence, locked, _ = await provisioning.lock_connector(
        session,
        source.id,
        provisioning._ALL_CREDENTIAL_SLOTS,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if source_fence is None or locked is None:
        raise NotFoundError("Source not found")
    error = await activate_native_in_uow(
        session,
        source.id,
        source_fence,
        locked,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    if error:
        raise ConflictError(error)
    await scheduler.upsert_schedule(
        session,
        workspace_id=source.workspace_id,
        source_id=source.id,
        interval_minutes=_DISABLED_INTERVAL_MINUTES,
        enabled=False,
    )


async def provision_topic_watch_source(
    session: AsyncSession,
    *,
    name: str,
    config: TopicWatchConfig,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> UUID:
    """Create the owner's Topic watch Source, configure it, activate it and return its id.

    The Source is a private ``web``/``topic_watch`` source named ``name`` (truncated to 200).
    Source creation commits inside the sources owner; configuration, activation and the disabled
    schedule are then flushed in the caller's transaction. Raises ``ConflictError`` with a bounded
    owner-action code when activation is refused (for example ``source_inactive``).
    """
    created = await sources.create_source(
        session,
        SourceCreate(type="web", provider="topic_watch", name=name[:200]),
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    source = await sources.get_connector_source(
        session, created.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if source is None:
        raise NotFoundError("Source not found")
    await _save_and_activate(
        session, source, config, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return source.id


async def update_topic_watch_source(
    session: AsyncSession,
    source_id: UUID,
    config: TopicWatchConfig,
    *,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> None:
    """Rewrite a watch Source's configuration, re-activate it and keep its schedule disabled.

    Raises ``NotFoundError`` for a missing or foreign Source and ``ConflictError`` as in provision.
    Flush-only; the caller commits.
    """
    source = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    if source is None or source.provider != "topic_watch":
        raise NotFoundError("Source not found")
    await _save_and_activate(
        session, source, config, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def retire_topic_watch_source(
    session: AsyncSession,
    source_id: UUID,
    *,
    purge_items: bool,
    scope: WorkspaceContext,
    multi_workspace_enabled: bool,
) -> None:
    """Retire a watch Source: archive it (items stay searchable) or queue a purge of its items.

    ``purge_items=True`` uses the sources purge operation, which archives the Source and deletes
    its Documents asynchronously. Both owners commit their own work; a missing Source is a no-op.
    """
    if purge_items:
        await sources.start_source_purge(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
    else:
        await sources.archive_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
