"""Connector configuration reads/writes and collection/credential fence checks.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
)
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.connectors.backends import (
    NATIVE_PROVIDERS,
    PROVIDER_SCOPE_FIELDS,
    native_dispatch_supported,
)
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorOAuthGrant,
    ConnectorProvisioning,
    ConnectorRestCredential,
    ConnectorWorldCredential,
)
from modules.connectors.provider_options import (
    option_fields,
)
from modules.connectors.provider_specs import (
    COLLECTION_FILTER_FIELDS,
    INCREMENTAL_PROVIDERS,
    min_interval_minutes,
)
from modules.connectors.sections import SECTION_CATALOG
from modules.connectors.service.access import (
    _read_scoped_source,
)
from modules.connectors.service.schemas import (
    DEFAULT_TIMEZONE,
    CollectionFence,
    ConnectorConfig,
    ConnectorConfigurationSnapshot,
    NativeCredentialSnapshot,
)
from modules.sources.public import ConnectorSource, SourceFence

_OAUTH_PROVIDERS = frozenset({"google_mail", "google_calendar", "google_drive", "reddit"})


def _projected_configuration(source: ConnectorSource) -> dict[str, Any]:
    """Project a Source's stored configuration into the owner-visible JSON snapshot.

    Validates through ``ConnectorConfig``; native providers keep only their scope, common,
    filter, option and section fields and get their defaults (history mode, timezone, interval,
    30 s timeout when unset). Every provider gets a default schedule interval when absent.
    """
    configuration = ConnectorConfig.model_validate(source.configuration).model_dump(
        mode="json", exclude_none=True
    )
    if source.provider in NATIVE_PROVIDERS:
        scope_fields = PROVIDER_SCOPE_FIELDS[source.provider]
        common = {
            "timezone", "schedule_interval_minutes", "timeout_seconds", "history_mode",
            "realtime_mode", "retention_days", "skip_embedding",
        }
        configuration = {
            key: value for key, value in configuration.items()
            if key in scope_fields | common | (COLLECTION_FILTER_FIELDS if source.provider in INCREMENTAL_PROVIDERS else set())
            | option_fields(source.provider)
            | ({"feed_sections"} if source.provider in SECTION_CATALOG else set())
        }
        configuration.setdefault("history_mode", "pending_updates" if source.provider == "telegram" else "returned_snapshot")
        configuration.setdefault("timezone", DEFAULT_TIMEZONE)
        configuration.setdefault(
            "schedule_interval_minutes",
            max(default_schedule_interval_minutes(source.type), min_interval_minutes(source.provider)),
        )
        if "timeout_seconds" not in source.configuration:
            configuration["timeout_seconds"] = 30
    if "schedule_interval_minutes" not in configuration:
        configuration["schedule_interval_minutes"] = default_schedule_interval_minutes(
            source.type
        )
    return configuration


def _auth_method(provider: str | None, desired: dict[str, Any]) -> str:
    """Name the authentication method a provider uses; non-native providers read it from the draft."""
    return (
            "telegram_bot_token" if provider == "telegram"
            else "oauth2" if provider in {"google_mail", "google_calendar", "google_drive", "reddit"}
            else "http_header" if provider in {"notion", "slack"}
            else "http_header" if provider in {"health", "home_assistant"}
            else "none" if provider in NATIVE_PROVIDERS
            else str(desired.get("auth_method", "none"))
    )


def _provider_credential_configured(
    source: ConnectorSource, row: ConnectorProvisioning | None, scope: Scope, *,
    native_credential: ConnectorNativeCredential | None, oauth_grant: ConnectorOAuthGrant | None,
    world_credential: ConnectorWorldCredential | None, rest_credential: ConnectorRestCredential | None,
    provider_credential: ConnectorManagedCredential | None,
) -> bool:
    """Return whether the credential the provider needs is ready and bound to this exact revision.

    Telegram, OAuth providers and Alpha Vantage each require generation and revision agreement
    with the provisioning row; REST-credential sources and managed-credential sources require a
    ready credential.
    """
    return (
            bool(native_credential is not None and native_credential.state == "ready"
                 and native_credential.encrypted_token and native_credential.verified_bot_id
                 and native_credential.validated_at
                 and native_credential.source_generation == source.generation
                 and row is not None
                 and native_credential.configuration_revision == row.desired_revision)
            if source.provider == "telegram"
            else bool(oauth_grant is not None and oauth_grant.state == "ready" and oauth_grant.encrypted_tokens
                      and oauth_grant.provider == source.provider and oauth_grant.workspace_id == source.workspace_id
                      and oauth_grant.owner_id == (
                          scope.actor_user_id if isinstance(scope, InternalJobScope) else scope.user_id
                      ) and oauth_grant.source_generation == source.generation
                      and row is not None and oauth_grant.configuration_revision == row.desired_revision
                      and oauth_grant.credential_revision == row.credential_revision)
            if source.provider in {"google_mail", "google_calendar", "google_drive", "reddit"}
            else bool(world_credential is not None and row is not None
                      and world_credential.source_generation == source.generation
                      and world_credential.configuration_revision == row.desired_revision)
            if source.provider == "alpha_vantage"
            else bool(rest_credential is not None and rest_credential.state == "ready" and row is not None
                      and rest_credential.source_generation == source.generation
                      and rest_credential.configuration_revision == row.desired_revision)
            if rest_credential is not None
            else bool(provider_credential is not None and provider_credential.state == "ready"
                      and provider_credential.credential_id)
    )


def _provider_credential_state(
    source: ConnectorSource, row: ConnectorProvisioning | None, *,
    native_credential: ConnectorNativeCredential | None, oauth_grant: ConnectorOAuthGrant | None,
    world_credential: ConnectorWorldCredential | None, rest_credential: ConnectorRestCredential | None,
    provider_credential: ConnectorManagedCredential | None,
) -> str | None:
    """Report the credential lifecycle state shown to the owner, or ``None`` when nothing applies."""
    return (
oauth_grant.state if source.provider in {"google_mail", "google_calendar", "google_drive", "reddit"}
                                   and oauth_grant is not None else "not_connected" if source.provider in {"google_mail", "google_calendar", "google_drive", "reddit"}
                                   else (rest_credential.state if rest_credential is not None
                                         and rest_credential.state == "ready" and row is not None
                                         and rest_credential.source_generation == source.generation
                                         and rest_credential.configuration_revision == row.desired_revision
                                         else "not_connected") if source.provider in {"notion", "slack"}
                                   else "ready" if source.provider == "alpha_vantage" and world_credential is not None
                                   and row is not None and world_credential.source_generation == source.generation
                                   and world_credential.configuration_revision == row.desired_revision
                                   else native_credential.state if source.provider == "telegram" and native_credential is not None
                                   else "ready" if rest_credential is not None and rest_credential.state == "ready"
                                   else provider_credential.state if provider_credential is not None else None
    )


async def get_connector_configuration(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ConnectorConfigurationSnapshot | None:
    """Read a fresh scoped owner-safe configuration projection without acquiring row locks."""
    from modules.connectors import provisioning
    from modules.sources import public as sources

    source_fence, row, credentials = await provisioning._read_connector_rows(
        session, source_id, ("provider",),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source_fence is None:
        return None
    source = await sources.get_connector_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        return None
    if source.generation != source_fence.generation:
        raise RuntimeError("Locked source snapshot generation mismatch")
    provider_credential = credentials.get("provider")
    native_credential = None
    if source.provider == "telegram" and row is not None:
        native_credential = await session.scalar(select(ConnectorNativeCredential).where(
            ConnectorNativeCredential.source_id == source_id,
            ConnectorNativeCredential.source_generation == source.generation,
            ConnectorNativeCredential.configuration_revision == row.desired_revision,
        ).execution_options(populate_existing=True))
    world_credential = await session.get(ConnectorWorldCredential, source_id) if source.provider == "alpha_vantage" else None
    rest_credential = (
        await session.get(ConnectorRestCredential, source_id)
        if row is not None and "native" in (row.execution_backend, row.target_backend)
        else None
    )
    oauth_grant = await session.get(ConnectorOAuthGrant, source_id) if source.provider in {
        "google_mail", "google_calendar", "google_drive", "reddit",
    } else None
    configuration = _projected_configuration(source)
    desired = row.desired_configuration if row is not None else {}
    return ConnectorConfigurationSnapshot(
        workspace_id=source.workspace_id, source_id=source.id,
        source_type=source.type,
        provider=source.provider,
        source_generation=source.generation,
        configuration=configuration,
        expected_revision=row.desired_revision if row is not None else 0,
        auth_method=_auth_method(source.provider, desired),
        auth_header_name=(
            str(desired["auth_header_name"])
            if desired.get("auth_header_name") is not None
            else "Authorization" if source.provider in {"notion", "slack"}
            else None
        ),
        desired_enabled=bool(row and row.desired_enabled),
        activation_state=row.state if row is not None else "saved_not_active",
        activation_error_code=row.error_code if row is not None else None,
        provider_credential_configured=_provider_credential_configured(
            source, row, scope, native_credential=native_credential, oauth_grant=oauth_grant,
            world_credential=world_credential, rest_credential=rest_credential,
            provider_credential=provider_credential,
        ),
        provider_credential_state=_provider_credential_state(
            source, row, native_credential=native_credential, oauth_grant=oauth_grant,
            world_credential=world_credential, rest_credential=rest_credential,
            provider_credential=provider_credential,
        ),
        execution_backend=(row.execution_backend if row is not None else "native" if native_dispatch_supported(source.type, source.provider) else "n8n"),
        transition_phase=row.transition_phase if row is not None else "idle",
        target_backend=row.target_backend if row is not None else None,
    )


def default_schedule_interval_minutes(source_type: str) -> int:
    """Return the default polling cadence for RSS versus other source types."""
    return 15 if source_type == "rss" else 30


_UNSET = object()


# Provider-only ConnectorConfig fields whose model default is not None, so exclude_none cannot drop them.
_NATIVE_ONLY_DEFAULTS: dict[str, object] = {
    "include_issues": False, "include_pulls": False, "include_commits": False, "include_releases": False,
    "github_history_days": 90, "google_mail_history_days": 30,
}


def serialize_source_configuration(source: ConnectorSource, config: ConnectorConfig) -> dict[str, object]:
    """Serialize generic settings unchanged and native scopes with a reusable 30-second default.

    Pydantic's generic timeout default remains 60 seconds. For native adapters,
    an omitted timeout selects 30 seconds; an explicitly supplied value above
    30 is rejected so GET/PUT round-trips cannot accidentally reject the default.
    """
    values = config.model_dump(mode="json", exclude_none=True)
    if source.provider not in NATIVE_PROVIDERS:
        # Native-only fields with non-None defaults (GitHub flags/horizon) would otherwise be stored on every
        # RSS/web/REST source and make registry.validate reject it as carrying provider scope.
        return {key: value for key, value in values.items() if _NATIVE_ONLY_DEFAULTS.get(key, _UNSET) != value}
    scope_fields = PROVIDER_SCOPE_FIELDS[source.provider]
    common = {
        "timezone", "schedule_interval_minutes", "timeout_seconds", "history_mode",
        "realtime_mode", "retention_days", "skip_embedding",
    }
    if source.provider in INCREMENTAL_PROVIDERS:
        common |= COLLECTION_FILTER_FIELDS  # selection options are legal only for news/event built-ins
    if source.provider in SECTION_CATALOG:
        common |= {"feed_sections"}  # RSS sections are legal only where a server catalog exists
    common |= option_fields(source.provider)  # wave D per-type options, legal only on their own provider
    supplied = config.model_fields_set
    if supplied - scope_fields - common:
        raise ValueError("Native provider configuration contains unsupported fields")
    expected_history = "pending_updates" if source.provider == "telegram" else "returned_snapshot"
    if values.get("history_mode", expected_history) != expected_history:
        raise ValueError("Provider history mode does not match its collection contract")
    timeout_seconds = config.timeout_seconds if "timeout_seconds" in supplied else 30
    if timeout_seconds > 30:
        raise ValueError("Native provider request timeout cannot exceed 30 seconds")
    serialized = {key: value for key, value in values.items() if key in scope_fields | common}
    serialized["history_mode"] = expected_history
    serialized.setdefault("timezone", DEFAULT_TIMEZONE)
    serialized.setdefault(
        "schedule_interval_minutes",
        max(default_schedule_interval_minutes(source.type), min_interval_minutes(source.provider)),
    )
    serialized["timeout_seconds"] = timeout_seconds
    return serialized


async def get_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> NativeCredentialSnapshot | None:
    """Read a detached native credential under caller-held access/Source/provisioning locks.

    The credential owner acquires only the not-yet-held native row. Snapshot includes
    original AccessFence/workspace; ciphertext remains private to Ingestion/Connector owners.
    """
    from modules.connectors import provisioning

    return await provisioning.get_native_credential_snapshot(
        session,
        source_id,
        source_generation=source_generation,
        connector_revision=connector_revision,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def collection_allowed(
    session: AsyncSession,
    source_id: UUID,
    source_status: str,
    source_generation: int,
    fence: CollectionFence,
    *,
    lock: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Check source and connector revisions, optionally locking the provisioning row."""
    from modules.connectors import provisioning

    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or source.status != source_status or source.generation != source_generation:
        return False
    return await provisioning.require_collection_fence(
        session, source, fence.source_generation, fence.connector_revision, lock=lock,
        backend_revision=fence.backend_revision, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def require_collection_fence(
    session: AsyncSession,
    source: ConnectorSource,
    fence: CollectionFence,
    *,
    lock: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Enforce the supplied collection fence through connector provisioning state."""
    from modules.connectors import provisioning

    return await provisioning.require_collection_fence(
        session,
        source,
        fence.source_generation,
        fence.connector_revision,
        lock=lock, backend_revision=fence.backend_revision,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def validate_provider_credential_revision_in_uow(
    session: AsyncSession, source_id: UUID, expected_revision: int, *,
    access_fence: AccessFence, source_fence: SourceFence, scope: Scope,
    multi_workspace_enabled: bool,
) -> bool:
    """Prove a credential-backed adapter's captured revision under connector owner locks.

    The caller already holds the source/access locks and commits the receipt in this unit of work;
    a credential replacement or disconnect bumps the provisioning revision and makes old output fail.
    """
    from modules.connectors import provisioning

    current, row, _ = await provisioning.lock_connector(
        session, source_id, provisioning._ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence,
    )
    if current != source_fence or row is None or row.credential_revision != expected_revision:
        return False
    credential = await session.get(ConnectorRestCredential, source_id, with_for_update=True, populate_existing=True)
    return bool(
        credential is not None and credential.state == "ready" and credential.encrypted_secret
        and credential.source_generation == source_fence.generation
        and credential.configuration_revision == row.desired_revision
        and credential.header_name == "Authorization"
    )


async def validate_world_credential_operation_in_uow(
    session: AsyncSession, source_id: UUID, *, source_generation: int, connector_revision: int,
    expected_operation_id: UUID, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Prove original Alpha key operation by locking only its exact world credential row.

    Caller holds original admission/Source/provisioning and required earlier managed rows;
    enter before Ingestion credentials/Tools/GitHub/state. Fresh complete access/Source and
    fully applied active collection proof must match, otherwise409 (typed admission errors
    propagate). Missing/changed credential returns False, never a new current operation.
    Compare provider, Source/G/Connector R, UUID and nonempty encrypted key under fresh row
    lock. No fictitious state/token revision, decrypt/key return, mutation, commit or I/O.
    """
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if (source is None or source.status != "active" or source.local_only
            or source.provider != "alpha_vantage" or source.generation != source_generation
            or source_fence.id != source_id or source_fence.generation != source_generation
            or not isinstance(expected_operation_id, UUID)):
        raise ConflictError("World credential collection fence changed")
    if not await require_collection_fence(
        session, source,
        CollectionFence(source_generation=source_generation, connector_revision=connector_revision),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    ):
        raise ConflictError("World credential provisioning fence changed")
    credential = await session.scalar(select(ConnectorWorldCredential).where(
        ConnectorWorldCredential.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    return bool(
        credential is not None and credential.provider == "alpha_vantage"
        and credential.source_id == source_id and credential.source_generation == source_generation
        and credential.configuration_revision == connector_revision
        and credential.operation_id == expected_operation_id
        and isinstance(credential.encrypted_key, str) and credential.encrypted_key
    )


async def require_batch_fence(
    session: AsyncSession,
    source: ConnectorSource,
    source_generation: int,
    connector_revision: int | None,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Require a revision for managed connector sources while preserving native ingestion."""
    from modules.connectors import provisioning

    current_source = await _read_scoped_source(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if current_source != source or source.status != "active" or source.generation != source_generation:
        return False
    row = await provisioning.activation_status(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if row is None:
        return connector_revision is None and source.generation == source_generation
    if connector_revision is None:
        return False
    return await provisioning.require_collection_fence(
        session, source, source_generation, connector_revision, lock=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )


async def fence_source_collection(session: AsyncSession, source: SourceFence, *, scope: Scope, multi_workspace_enabled: bool) -> bool:
    """Persist connector-owned deactivation after the source owner has fenced a source."""
    from modules.connectors import provisioning

    return await provisioning.fence_source_collection(session, source, scope=scope, multi_workspace_enabled=multi_workspace_enabled)


async def save_connector_configuration(
    session: AsyncSession,
    source: ConnectorSource,
    expected_revision: int,
    source_configuration: dict[str, object],
    desired_configuration: dict[str, object],
    *,
    allow_paused: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
) -> tuple[ConnectorSource, ConnectorProvisioning] | None:
    """Save source and desired connector settings, rolling back revision conflicts."""
    from modules.connectors import provisioning
    from modules.sources import public as source_public

    saved = await source_public.set_connector_configuration(
        session, source.id, source.generation, source_configuration,
        allow_paused=allow_paused,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if saved is None:
        return None
    row = await provisioning._save_desired_in_uow(
        session,
        source.id,
        saved.generation,
        expected_revision,
        desired_configuration,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if row is None:
        await session.rollback()
        return None
    return saved, row


async def allow_external_collector_credential_issue(
    session: AsyncSession, source_id: UUID,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Allow external token issuance only for active sources not under provisioning."""
    from modules.connectors import provisioning

    source, row, _ = await provisioning.lock_connector(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return bool(
        source is not None and source.status == "active"
        and (row is None or (not row.desired_enabled and row.state != "provisioning"))
    )
