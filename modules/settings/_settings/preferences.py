"""Owner preferences, module availability, translation settings and AI policy fingerprint."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.models import Owner
from core.auth.schemas import AccountSessionRef
from core.config import Settings
from core.errors import (
    ConflictError,
)
from core.workspaces.public import (
    resolve_workspace_owner_context,
)
from core.workspaces.schemas import Scope
from modules.settings._settings.common import (
    _fingerprint,
    derive_for_role,
    require_preferences_account,
)
from modules.settings._settings.execution import get_ai_execution_config
from modules.settings.display_name import derive_display_name, normalize_display_name
from modules.settings.models import (
    OwnerPreferencesRecord,
    TranslationSettingsRecord,
)
from modules.settings.schemas import (
    ModuleLifecycleRead,
    OwnerPreferencesRead,
    OwnerPreferencesUpdate,
    RetentionSettingsRead,
    TranslationSettingsRead,
)


async def _resolved_display_name(session: AsyncSession, owner_id: int, stored: str | None) -> str:
    """Return the stored name, else one derived from the owner's email (never empty); read-only."""
    if stored:
        return stored
    return derive_display_name(
        await session.scalar(select(Owner.email).where(Owner.id == owner_id))
    )


async def set_initial_owner_display_name(
    session: AsyncSession, *, owner_id: int, display_name: str | None
) -> None:
    """Store the first-run name in the caller's transaction (no commit); blank/None is a no-op.

    Creates the preferences row with column defaults. Used by setup right after the owner row is
    flushed, so the owner and name commit or roll back together.
    """
    name = normalize_display_name(display_name)
    if name is None:
        return
    await session.execute(
        insert(OwnerPreferencesRecord)
        .values(owner_id=owner_id, display_name=name)
        .on_conflict_do_update(index_elements=["owner_id"], set_={"display_name": name})
    )


async def read_owner_preferences(
    session: AsyncSession, *, actor_user_id: int, multi_workspace_enabled: bool
) -> OwnerPreferencesRead:
    """Return active account preferences/defaults; selected workspace is irrelevant."""
    await require_preferences_account(
        session, actor_user_id=actor_user_id, multi_workspace_enabled=multi_workspace_enabled
    )
    row = await session.scalar(
        select(OwnerPreferencesRecord)
        .where(OwnerPreferencesRecord.owner_id == actor_user_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return OwnerPreferencesRead(
            configuration_revision=1,
            persisted=False,
            theme="system",
            locale="en-us",
            timezone="Asia/Ho_Chi_Minh",
            resolved_display_name=await _resolved_display_name(session, actor_user_id, None),
        )
    return OwnerPreferencesRead(
        configuration_revision=row.configuration_revision,
        persisted=True,
        theme=row.theme,
        locale=row.locale,
        timezone=row.timezone,
        display_name=row.display_name,
        resolved_display_name=await _resolved_display_name(
            session, actor_user_id, row.display_name
        ),
        accent=row.accent,
    )


async def read_module_availability(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> ModuleLifecycleRead:
    """Return owner-only scoped module DTO with actual configured gate; never mutate process state."""
    from modules.settings.lifecycle import read_module_lifecycle

    return await read_module_lifecycle(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def read_retention_settings(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> RetentionSettingsRead:
    """Return admitted workspace retention DTO; workers must bind the same scope to cleanup."""
    from modules.settings.lifecycle import read_retention_settings as _read_retention_settings

    return await _read_retention_settings(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


async def module_is_enabled(
    session: AsyncSession, module_id: str, *, scope: Scope, multi_workspace_enabled: bool
) -> bool:
    """Resolve admitted owner workspace availability for jobs/tools, never global settings."""
    from modules.settings.lifecycle import module_is_enabled as _module_is_enabled

    return await _module_is_enabled(
        session, module_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


def module_dependency(module_id: str) -> Callable[..., Awaitable[None]]:
    """Build scoped availability dependency; domain owner separately enforces resource grants."""
    from modules.settings.lifecycle import module_dependency as _module_dependency

    return _module_dependency(module_id)


async def save_owner_preferences(
    session: AsyncSession,
    update: OwnerPreferencesUpdate,
    *,
    actor_user_id: int,
    multi_workspace_enabled: bool,
    auth_sessions: tuple[AccountSessionRef, ...] = (),
) -> OwnerPreferencesRead:
    """Persist actual account preferences with exact-session auth admission and CAS.

    Creates then locks the actor row to serialize first writes; transaction
    completion remains the caller's responsibility.
    """
    await require_preferences_account(
        session,
        actor_user_id=actor_user_id,
        multi_workspace_enabled=multi_workspace_enabled,
        locked=True,
        auth_sessions=auth_sessions,
    )
    await session.execute(
        insert(OwnerPreferencesRecord)
        .values(owner_id=actor_user_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    row = await session.scalar(
        select(OwnerPreferencesRecord)
        .where(OwnerPreferencesRecord.owner_id == actor_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise RuntimeError("Owner preferences singleton could not be initialized")
    if row.configuration_revision != update.expected_revision:
        raise ConflictError(detail="Preferences changed; reload before saving")
    row.theme = update.theme
    row.locale = update.locale
    row.timezone = update.timezone
    row.display_name = update.display_name
    row.accent = update.accent
    row.configuration_revision += 1
    await session.flush()
    return OwnerPreferencesRead(
        configuration_revision=row.configuration_revision,
        persisted=True,
        theme=row.theme,
        locale=row.locale,
        timezone=row.timezone,
        display_name=row.display_name,
        resolved_display_name=await _resolved_display_name(
            session, actor_user_id, row.display_name
        ),
        accent=row.accent,
    )


async def get_translation_settings(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool
) -> TranslationSettingsRead:
    """Workspace translation choice for T2/T3 consumers; member-safe, admission before query."""
    from modules.translations.public import (
        read_translation_settings,  # lazy: avoids a settings<->translations import cycle
    )

    return await read_translation_settings(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )


def _translation_settings_read(row: TranslationSettingsRecord | None) -> TranslationSettingsRead:
    """Project a settings row (or the documented default for an absent row) to the public DTO."""
    if row is None:
        return TranslationSettingsRead()
    return TranslationSettingsRead(
        enabled=row.enabled,
        target_language=row.target_language,
        configuration_revision=row.configuration_revision,
    )


async def read_translation_settings_projection(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    lock: bool = False,
) -> TranslationSettingsRead:
    """Read one workspace's translation choice for the Translations module (owner: Settings).

    Read-only projection of ``translation_settings`` (fields: enabled, target_language, configuration_revision). The
    caller must already have admitted the workspace (access fence); this function does not authorize. An absent row
    yields the documented default (disabled, ``vi``, revision 1). ``lock=True`` adds ``FOR UPDATE`` so the caller
    serializes against a concurrent settings save. ``populate_existing`` refreshes any identity-mapped row.
    """
    statement = select(TranslationSettingsRecord).where(
        TranslationSettingsRecord.workspace_id == workspace_id
    )
    if lock:
        statement = statement.with_for_update()
    return _translation_settings_read(
        await session.scalar(statement.execution_options(populate_existing=True))
    )


async def save_translation_settings_cas(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    enabled: bool,
    target_language: str,
    expected_revision: int,
    updated_by_user_id: int,
) -> tuple[TranslationSettingsRead, bool]:
    """Compare-and-set one workspace's translation choice; the caller commits and holds the access fence.

    Inserts the row on first use (``ON CONFLICT DO NOTHING``), locks it ``FOR UPDATE`` and rejects a stale
    ``expected_revision`` with ``ConflictError``. Returns ``(settings, changed)``: when nothing changed the revision is
    untouched and ``changed`` is False; otherwise the revision is bumped and ``changed`` is True, and the caller must
    block its own derived pending work. The write is flushed lazily by the caller's next statement or flush.
    """
    await session.execute(
        insert(TranslationSettingsRecord)
        .values(workspace_id=workspace_id)
        .on_conflict_do_nothing(index_elements=["workspace_id"])
    )
    row = await session.scalar(
        select(TranslationSettingsRecord)
        .where(
            TranslationSettingsRecord.workspace_id == workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise RuntimeError("Translation settings could not be initialized")
    if row.configuration_revision != expected_revision:
        raise ConflictError(detail="Translation settings changed; reload before saving")
    if row.enabled == enabled and row.target_language == target_language:
        return _translation_settings_read(row), False
    row.enabled = enabled
    row.target_language = target_language
    row.configuration_revision += 1
    row.updated_by_user_id = updated_by_user_id
    return _translation_settings_read(row), True


async def read_ai_policy_fingerprint(
    session: AsyncSession,
    settings: Settings,
    redis: Redis | None,
    *,
    workspace_id: UUID,
    role: str,
) -> str | None:
    """Nonsecret digest of the workspace owner's AI execution policy for one role, or None when unusable.

    Members cannot read Settings, so the owner context is resolved internally. Covers endpoint identity,
    credential fingerprint, role mapping version, remote/privacy flags and AI settings revision.
    """
    owner = await resolve_workspace_owner_context(
        session, workspace_id, multi_workspace_enabled=settings.multi_workspace_enabled
    )
    if owner is None:
        return None
    config = derive_for_role(
        await get_ai_execution_config(session, settings, redis, scope=owner), role
    )
    mapping = config.defaults.resolve(role)
    if mapping is None or config.gateway_base_url is None or config.endpoint_policy_denied:
        return None
    destination = config.endpoint_destination_id
    if destination is None:
        return None
    # Local providers need neither a credential nor remote consent.
    if not destination.startswith("local:") and not (
        config.gateway_credential_configured and config.privacy.allow_remote_reasoning
    ):
        return None
    return hashlib.sha256(
        json.dumps(
            (
                destination,
                _fingerprint(config.gateway_api_key),
                role,
                mapping.model,
                mapping.version,
                mapping.destination,
                config.privacy.model_dump(mode="json"),
                config.configuration_revision,
            ),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
