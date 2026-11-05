"""Connector observation capture and commit (post-lock source/row snapshots)."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import ConflictError
from core.realtime import ReplayDraft, commit_with_replay, make_source_change
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
)
from modules.connectors.provisioning._common import _ALL_CREDENTIAL_SLOTS
from modules.connectors.provisioning._status import lock_connector
from modules.connectors.public import (
    _connector_access,
)
from modules.sources import public as sources
from modules.sources.public import SourceFence


@dataclass(frozen=True)
class ConnectorObservation:
    """Capture owner-visible connector state while omitting credential material."""

    fence: SourceFence
    access_fence: AccessFence
    desired_revision: int
    applied_revision: int
    state: str
    error_code: str | None
    credential_recovery: str
    desired_enabled: bool
    credential_presence: tuple[tuple[str, bool], ...]
    header_auth_configured: bool


def _connector_observation(
    source: SourceFence | None,
    row: ConnectorProvisioning | None,
    slots: dict[str, ConnectorManagedCredential],
    access_fence: AccessFence,
) -> ConnectorObservation | None:
    """Project locked connector rows into a redacted observable state snapshot."""
    if source is None:
        return None
    if row is None:
        return ConnectorObservation(
            fence=source,
            access_fence=access_fence,
            desired_revision=0,
            applied_revision=0,
            state="saved_not_active",
            error_code=None,
            credential_recovery="supported",
            desired_enabled=False,
            credential_presence=(),
            header_auth_configured=False,
        )
    unresolved = any(
        credential.state in {"dispatching", "reconciliation_required", "delete_pending"}
        for credential in slots.values()
    )
    state = row.state
    error_code = row.error_code
    if unresolved:
        error_code = "credential_operation_pending"
        if state != "disabled":
            state = "reconciliation_required"
    desired_configuration = row.desired_configuration
    return ConnectorObservation(
        fence=source,
        access_fence=access_fence,
        desired_revision=row.desired_revision,
        applied_revision=row.applied_revision,
        state=state,
        error_code=error_code,
        credential_recovery="unsupported_operation" if unresolved else "supported",
        desired_enabled=row.desired_enabled,
        credential_presence=tuple(
            (slot, bool(slots.get(slot) and slots[slot].credential_id))
            for slot in _ALL_CREDENTIAL_SLOTS
        ),
        header_auth_configured=bool(
            isinstance(desired_configuration, dict)
            and desired_configuration.get("auth_method") == "http_header"
        ),
    )


async def capture_connector_observation(
    session: AsyncSession,
    source_id: UUID,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> ConnectorObservation | None:
    """Read a coherent source/provisioning/credential snapshot under lock order."""
    source, row, slots = await lock_connector(
        session,
        source_id,
        _ALL_CREDENTIAL_SLOTS,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    access_fence = await _connector_access(
        session, scope=scope, multi_workspace_enabled=multi_workspace_enabled
    )
    return _connector_observation(source, row, slots, access_fence)


async def commit_connector_observation(
    session: AsyncSession,
    before: ConnectorObservation | None,
    *,
    operation_id: UUID | None = None,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> None:
    """Commit connector state and publish a source event when its safe view changed.

    Always flushes and commits the caller's session, even when the redacted
    snapshot is unchanged and no realtime event is emitted. ``operation_id``
    correlates an emitted source change with its durable provider operation.
    With ``before=None`` there is no comparable snapshot and no event draft.
    """
    access_fence = await _connector_access(
        session,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=before.access_fence if before is not None else None,
    )
    if (
        before is not None
        and await sources.get_source_fence(
            session, before.fence.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled
        )
        != before.fence
    ):
        raise ConflictError("Connector observation Source changed")
    await session.flush()
    drafts: list[ReplayDraft] = []
    if before is not None:
        result = await session.execute(
            select(
                ConnectorProvisioning.desired_revision,
                ConnectorProvisioning.applied_revision,
                ConnectorProvisioning.state,
                ConnectorProvisioning.error_code,
                ConnectorProvisioning.desired_enabled,
                ConnectorProvisioning.desired_configuration,
            ).where(ConnectorProvisioning.source_id == before.fence.id)
        )
        row = result.one_or_none()
        credential_rows = list(
            (
                await session.execute(
                    select(
                        ConnectorManagedCredential.slot,
                        ConnectorManagedCredential.credential_id.is_not(None),
                        ConnectorManagedCredential.state,
                    ).where(ConnectorManagedCredential.source_id == before.fence.id)
                )
            ).all()
        )
        if row is None:
            after = ConnectorObservation(
                fence=before.fence,
                access_fence=access_fence,
                desired_revision=0,
                applied_revision=0,
                state="saved_not_active",
                error_code=None,
                credential_recovery="supported",
                desired_enabled=False,
                credential_presence=(),
                header_auth_configured=False,
            )
        else:
            values = {slot: (present, state) for slot, present, state in credential_rows}
            unresolved = any(
                state in {"dispatching", "reconciliation_required", "delete_pending"}
                for _present, state in values.values()
            )
            state = row.state
            error_code = row.error_code
            if unresolved:
                error_code = "credential_operation_pending"
                if state != "disabled":
                    state = "reconciliation_required"
            desired_configuration = row.desired_configuration
            after = ConnectorObservation(
                fence=before.fence,
                access_fence=access_fence,
                desired_revision=row.desired_revision,
                applied_revision=row.applied_revision,
                state=state,
                error_code=error_code,
                credential_recovery="unsupported_operation" if unresolved else "supported",
                desired_enabled=row.desired_enabled,
                credential_presence=tuple(
                    (slot, bool(values.get(slot, (False, "queued"))[0]))
                    for slot in _ALL_CREDENTIAL_SLOTS
                ),
                header_auth_configured=bool(
                    isinstance(desired_configuration, dict)
                    and desired_configuration.get("auth_method") == "http_header"
                ),
            )
        if after != before:
            drafts.append(
                make_source_change(
                    before.fence.id,
                    before.fence.generation,
                    before.fence.status,
                    connector_state=after.state,
                    operation_id=operation_id,
                    scope=scope,
                )
            )
    await commit_with_replay(
        session,
        drafts,
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence,
    )
