"""Native credential snapshot, save and revoke operations."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from core.ids import uuid7
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorNativeCredential,
)
from modules.connectors.provisioning._common import _ALL_CREDENTIAL_SLOTS, _read_connector_rows
from modules.connectors.provisioning._status import lock_connector
from modules.connectors.public import (
    NativeCredentialRevocationSnapshot,
    NativeCredentialSnapshot,
    _connector_access,
)
from modules.sources import public as sources


async def get_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> NativeCredentialSnapshot | None:
    """Lock only the native row under caller-held access/Source/provisioning collection proof.

    Fresh Source/provisioning reads acquire no earlier locks. Return a private detached
    snapshot only for exact active generation/revision; caller retains or releases its UoW.
    """
    source, row, _slots = await _read_connector_rows(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if (
        source is None or source.status != "active" or row is None or source.generation != source_generation
        or row.desired_revision != connector_revision or row.source_generation != source_generation
    ):
        return None
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return None
    if (
        native.source_generation != source_generation
        or native.configuration_revision != connector_revision
    ):
        return None
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return NativeCredentialSnapshot(
        access_fence=access_fence,
        workspace_id=source.workspace_id, source_id=native.source_id,
        operation_id=native.operation_id,
        source_generation=native.source_generation,
        configuration_revision=native.configuration_revision,
        verified_bot_id=native.verified_bot_id,
        bound_bot_id=native.bound_bot_id,
        encrypted_token=native.encrypted_token,
        state=native.state,
        validated_at=native.validated_at,
    )


async def get_retained_native_credential_snapshot(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool,
) -> NativeCredentialSnapshot | None:
    """Return a retained binding only under the active owner's requested source/revision fence.

    Source, provisioning, managed slots, and native row are locked in the normal
    connector order before an owner may decrypt or remotely revalidate a token.
    A missing native row returns None; a stale source or revision raises ValueError.
    """
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source.status != "active" or source.generation != source_generation:
        raise ValueError("Connector credential fence is stale")
    if row is None:
        if connector_revision == 0:
            return None
        raise ValueError("Connector credential fence is stale")
    if row.source_generation != source_generation or row.desired_revision != connector_revision:
        raise ValueError("Connector credential fence is stale")
    if row.state == "disabled" and row.error_code == "deactivation_pending":
        raise ValueError("Connector deactivation is pending")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if native is None:
        return None
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    return NativeCredentialSnapshot(
        access_fence=access_fence,
        workspace_id=source.workspace_id, source_id=native.source_id,
        operation_id=native.operation_id,
        source_generation=native.source_generation,
        configuration_revision=native.configuration_revision,
        verified_bot_id=native.verified_bot_id,
        bound_bot_id=native.bound_bot_id,
        encrypted_token=native.encrypted_token,
        state=native.state,
        validated_at=native.validated_at,
    )


async def get_native_credential_revocation_snapshot(
    session: AsyncSession, source_id: UUID, *, source_generation: int, connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> NativeCredentialRevocationSnapshot:
    """Acquire original admission/Source/provisioning/sorted slots then paused native capture.

    Enter without domain locks, retaining the initial browser/job AccessFence. Removal alone
    requires exact paused Telegram Source G and disabled provisioning G/desired R with no
    deactivation pending. Unavailable/stale lifecycle/revision or malformed provider row
    raises ValueError; typed admission failures propagate. True native absence yields None
    operation only after valid parents; an older stored native G/R yields its actual UUID.
    Detach original access/current paused fence/requested R without cipher/bot/readiness or
    provider authority. No mutation/commit/I/O; caller detaches other data before release and
    later saves/revokes with this original operation-or-absence CAS, never a fresh rescue.
    """
    source, row, _slots = await lock_connector(
        session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence,
    )
    source_view = await sources.get_connector_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if (source is None or source.id != source_id or source.workspace_id != scope.workspace_id
            or source.status != "paused" or source.generation != source_generation
            or source_view is None or source_view.provider != "telegram"
            or row is None or row.source_id != source_id or row.source_generation != source_generation
            or row.desired_revision != connector_revision or row.desired_enabled
            or row.state != "disabled" or row.error_code == "deactivation_pending"):
        raise ValueError("Paused native credential removal fence is stale")
    native = await session.scalar(select(ConnectorNativeCredential).where(
        ConnectorNativeCredential.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if native is not None and (
        native.source_id != source_id or native.provider != "telegram"
        or not isinstance(native.operation_id, UUID)
        or native.source_generation > source_generation or native.configuration_revision > connector_revision
    ):
        raise ValueError("Native credential removal identity is malformed")
    current_access = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    return NativeCredentialRevocationSnapshot(
        access_fence=current_access, source_fence=source, connector_revision=connector_revision,
        operation_id=native.operation_id if native is not None else None,
    )


async def save_native_credential(
    session: AsyncSession,
    *,
    source_id: UUID,
    operation_id: UUID,
    source_generation: int,
    connector_revision: int,
    encrypted_token: str,
    token_fingerprint: str,
    verified_bot_id: str,
    validated_at: datetime,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, expected_native_operation_id: UUID | None,
) -> None:
    """Acquire original access/Source/config and native-operation CAS before saving verified token.

    Caller supplies original AccessFence and prior native operation (None for observed absence)
    retained across verification I/O. Current row must still match; flush only, caller commits.
    """
    source, row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    if (
        source is None or row is None or source.generation != source_generation
        or row.source_generation != source_generation or row.desired_revision != connector_revision
    ):
        raise ValueError("Connector credential fence is stale")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (native.operation_id if native is not None else None) != expected_native_operation_id:
        raise ConflictError("Native credential operation changed")
    if native is not None and (
        native.bound_bot_id not in (None, verified_bot_id)
        or native.verified_bot_id not in (None, verified_bot_id)
    ):
        raise ValueError("Telegram bot identity cannot change for an existing source")
    if native is None:
        native = ConnectorNativeCredential(source_id=source_id, operation_id=operation_id)
        session.add(native)
    native.provider = "telegram"
    native.operation_id = operation_id
    native.source_generation = source_generation
    native.configuration_revision = connector_revision
    native.encrypted_token = encrypted_token
    native.token_fingerprint = token_fingerprint
    native.verified_bot_id = verified_bot_id
    # Keep historical identity separate from the unique live reservation.
    native.bound_bot_id = verified_bot_id
    native.state = "ready"
    native.validated_at = validated_at
    native.error_code = None
    row.credential_revision += 1
    await session.flush()


async def revoke_native_credential(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    release_bot_reservation: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, expected_native_operation_id: UUID | None,
) -> None:
    """Acquire original admission/Source/provisioning/slots before captured native revoke.

    Caller supplies the retained AccessFence/native operation; enter before domain locks.
    Clear ciphertext and optional live reservation while preserving historical bot binding.
    """
    _source, _row, _slots = await lock_connector(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    return await revoke_native_credential_in_uow(
        session, source_id, source_generation=source_generation, connector_revision=connector_revision,
        release_bot_reservation=release_bot_reservation, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        expected_native_operation_id=expected_native_operation_id,
    )


async def revoke_native_credential_in_uow(
    session: AsyncSession,
    source_id: UUID,
    *,
    source_generation: int,
    connector_revision: int,
    release_bot_reservation: bool = False,
    scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, expected_native_operation_id: UUID | None,
) -> None:
    """Revoke captured native operation under held admission/Source/provisioning/slot locks.

    Freshly compare original AccessFence and Source/config; lock only the native row and
    require original operation CAS. Clear ciphertext/reservation, preserve historical bot;
    flush only, caller owns commit. Do not enter after a native/grant/ingestion lock.
    """
    await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)
    source, row, _slots = await _read_connector_rows(session, source_id, _ALL_CREDENTIAL_SLOTS, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or row is None or source.generation != source_generation or row.desired_revision != connector_revision:
        raise ValueError("Connector credential fence is stale")
    native = await session.scalar(
        select(ConnectorNativeCredential)
        .where(ConnectorNativeCredential.source_id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (native.operation_id if native is not None else None) != expected_native_operation_id:
        raise ConflictError("Native credential operation changed")
    if native is None:
        return
    native.source_generation = source_generation
    native.configuration_revision = connector_revision
    native.operation_id = uuid7()
    native.encrypted_token = None
    native.token_fingerprint = None
    native.validated_at = None
    native.state = "revoked"
    native.error_code = None
    if release_bot_reservation:
        # The unique active reservation is reusable; the source's bot binding is immutable.
        native.verified_bot_id = None
    await session.flush()
