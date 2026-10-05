"""Async owner repository for revisioned MCP records and detached authorization fences."""

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import secrets
from uuid import UUID, uuid4

from sqlalchemy import Text, bindparam, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from modules.tools.mcp_credentials import (
    decrypt_connection_credential, encrypt_connection_credential,
    hash_inbound_token, issue_inbound_token, verify_inbound_token,
)
from modules.tools.mcp_schemas import (
    CapabilityRead, ConnectionDraft, ConnectionRead, DiscoveryPersist, DiscoveryRead,
    ExecutionFence, GrantChoice, GrantRead, InboundBinding, InboundClientCreate,
    InboundClientIssued, InboundClientRead, InboundPrincipal,
)
from modules.tools.models import McpCapability, McpCapabilityGrant, McpConnection, McpDiscovery, McpInboundClient


class McpConflict(Exception):
    """Raised when an optimistic revision, immutable descriptor, or uniqueness fence changed."""


class McpNotFound(Exception):
    """Raised when an owner-scoped MCP record does not exist."""


class McpUnavailable(Exception):
    """Raised when requested network behavior has no implemented real transport owner."""


def to_connection_read(row: McpConnection) -> ConnectionRead:
    """Project detached connection metadata and only the reviewed profile hash, never launch or credential data."""
    return ConnectionRead(
        id=row.id, name=row.name, transport=row.transport, endpoint=row.endpoint,
        deployment_profile_id=row.deployment_profile_id,
        deployment_profile_hash=row.deployment_profile_hash, revision=row.revision,
        enabled=row.enabled, auth_method=row.auth_method,
        credential_configured=row.encrypted_credential is not None,
        timeout_seconds=row.timeout_seconds, health=row.health_code,
        error_code=row.health_code, updated_at=row.updated_at,
    )


def to_discovery_read(row: McpDiscovery, capabilities: list[McpCapability]) -> DiscoveryRead:
    """Build an ordered detached result with copied descriptors and the server-captured profile hash."""
    return DiscoveryRead(
        id=row.id, connection_id=row.connection_id,
        connection_revision=row.connection_revision,
        deployment_profile_hash=row.deployment_profile_hash,
        protocol=row.negotiated_protocol,
        schema_set_hash=row.schema_set_hash,
        capabilities=tuple(CapabilityRead(
            id=item.id, kind=item.kind, remote_key=item.remote_key,
            descriptor=deepcopy(item.descriptor), descriptor_hash=item.descriptor_hash,
        ) for item in sorted(capabilities, key=lambda item: (item.kind, item.remote_key))),
        created_at=row.created_at,
    )


def to_grant_read(row: McpCapabilityGrant) -> GrantRead:
    """Project reviewed grant scope and profile identity without ORM state or secret fields."""
    from modules.tools.mcp_schemas import McpRisk
    return GrantRead(
        id=row.id, connection_id=row.connection_id, capability_id=row.capability_id,
        descriptor_hash=row.descriptor_hash,
        reviewed_connection_revision=row.reviewed_connection_revision,
        reviewed_profile_hash=row.reviewed_profile_hash,
        grant_revision=row.grant_revision, purpose=row.purpose, risk=McpRisk(row.risk),
        source_ids=tuple(UUID(value) for value in row.source_ids),
        destinations=tuple(row.destinations), expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def to_inbound_read(row: McpInboundClient) -> InboundClientRead:
    """Project token metadata and exact bindings without returning the token or hash."""
    return InboundClientRead(
        id=row.id, name=row.name, token_prefix=row.token_prefix, audience=row.audience,
        bindings=tuple(InboundBinding.model_validate(item) for item in row.bindings),
        source_ids=tuple(UUID(value) for value in row.source_ids),
        capabilities=tuple(row.capabilities), expires_at=row.expires_at,
        revoked_at=row.revoked_at, revision=row.revision,
    )


async def get_connection(session: AsyncSession, owner_id: int, connection_id: UUID, *, lock: bool = False) -> McpConnection:
    """Load one connection constrained by the authenticated owner, optionally acquiring its row lock."""
    statement = select(McpConnection).where(McpConnection.id == connection_id, McpConnection.owner_id == owner_id)
    if lock:
        statement = statement.with_for_update()
    statement = statement.execution_options(populate_existing=True)
    row = await session.scalar(statement)
    if row is None:
        raise McpNotFound("MCP connection not found")
    return row


async def list_connections(session: AsyncSession, owner_id: int) -> tuple[ConnectionRead, ...]:
    """Return at most 100 credential-free owner connections, newest first.

    The owner predicate is applied before the 101-ID sentinel query; rejecting that sentinel
    prevents an over-capacity catalog from being hydrated into ORM objects. Disabled rows count.
    The caller owns the read transaction and any rollback after a conflict.
    """
    ids = (await session.scalars(select(McpConnection.id).where(
        McpConnection.owner_id == owner_id,
    ).order_by(McpConnection.updated_at.desc(), McpConnection.id).limit(101))).all()
    if len(ids) > 100:
        raise McpConflict("MCP supports at most 100 connections")
    rows = []
    for connection_id in ids:
        rows.append(await get_connection(session, owner_id, connection_id))
    return tuple(to_connection_read(row) for row in rows)


async def save_connection(
    session: AsyncSession, owner_id: int, connection_id: UUID | None,
    expected_revision: int, draft: ConnectionDraft, *, encryption_key: str,
    deployment_profile_hash: str | None = None,
) -> ConnectionRead:
    """Create or revise an owner row using a server-selected stdio identity and safe credential state.

    Creation takes a nonblocking, owner-qualified PostgreSQL transaction advisory lock before
    counting persisted rows, so concurrent supported creates cannot pass the same capacity check.
    All saved rows count, including disabled drafts. A conflict occurs before insertion or
    credential work; the caller owns commit/rollback and therefore holds the lock through commit.
    Updates do not acquire the catalog lock and remain available at capacity. The route supplies
    the validated manifest hash; stdio cannot retain bearer material, and every update bumps the
    revision, disables the row, clears the successful-check hash and leaves old discoveries/grants stale.
    """
    if draft.transport.value == "stdio":
        if (deployment_profile_hash is None or len(deployment_profile_hash) != 64
                or any(char not in "0123456789abcdef" for char in deployment_profile_hash)):
            raise ValueError("stdio connection needs a current deployment profile identity")
    elif deployment_profile_hash is not None:
        raise ValueError("HTTP connections cannot bind a stdio deployment profile")
    now = datetime.now(UTC)
    if connection_id is None:
        if expected_revision != 0:
            raise McpConflict("New connection expected revision must be zero")
        # Stable dedicated MCP catalog namespace; xact scope serializes creates across app processes.
        if not await session.scalar(select(func.pg_try_advisory_xact_lock(1296257091, owner_id))):
            raise McpConflict("MCP connection catalog is busy; retry")
        owner_connection_ids = (await session.scalars(select(McpConnection.id).where(
            McpConnection.owner_id == owner_id).limit(100))).all()
        if len(owner_connection_ids) >= 100:
            raise McpConflict("MCP supports at most 100 connections")
        row = McpConnection(id=uuid4(), owner_id=owner_id, name=draft.name,
                            transport=draft.transport.value, endpoint=draft.endpoint,
                            deployment_profile_id=draft.deployment_profile_id,
                            deployment_profile_hash=deployment_profile_hash,
                            revision=1, credential_revision=1, updated_at=now)
        session.add(row)
    else:
        row = await get_connection(session, owner_id, connection_id, lock=True)
        if row.revision != expected_revision:
            raise McpConflict("Connection revision conflict")
        row.revision += 1
        row.enabled = False
        row.health_code = "needs_review"
        row.health_at = now
        # A revision change invalidates the old negotiation binding even when the profile ID is unchanged.
        row.deployment_profile_hash = deployment_profile_hash
        row.draft_check_profile_hash = None
    op = draft.credential_update
    if (draft.transport.value == "stdio" and connection_id is not None
            and row.encrypted_credential is not None and op.action != "remove"):
        raise ValueError("stdio connections cannot retain an existing bearer credential")
    if op.action == "replace":
        row.credential_revision += 1
        row.encrypted_credential = encrypt_connection_credential(
            op.value.get_secret_value(), key=encryption_key, owner_id=owner_id,
            connection_id=row.id, credential_revision=row.credential_revision)
    elif op.action == "remove":
        row.credential_revision += 1
        row.encrypted_credential = None
    elif connection_id is not None and op.action == "retain" and row.encrypted_credential is not None:
        # Rebind ciphertext to the new connection revision while preserving a valid secret.
        plaintext = decrypt_connection_credential(
            row.encrypted_credential, key=encryption_key, owner_id=owner_id,
            connection_id=row.id, credential_revision=row.credential_revision)
        row.credential_revision += 1
        row.encrypted_credential = encrypt_connection_credential(
            plaintext, key=encryption_key, owner_id=owner_id,
            connection_id=row.id, credential_revision=row.credential_revision)
    if draft.auth_method == "bearer" and row.encrypted_credential is None:
        raise ValueError("Bearer authentication requires a configured credential")
    row.name = draft.name
    row.transport = draft.transport.value
    row.endpoint = draft.endpoint
    row.deployment_profile_id = draft.deployment_profile_id
    row.auth_method = draft.auth_method
    row.timeout_seconds = draft.timeout_seconds
    row.updated_at = now
    # Caller owns commit; returned projection does not touch secret fields.
    await session.flush()
    return to_connection_read(row)


async def set_connection_enabled(session: AsyncSession, owner_id: int, connection_id: UUID, expected_revision: int, enabled: bool) -> ConnectionRead:
    """Flush enable state only for one matching check/discovery/grant identity chain.

    Stdio requires the successful check, latest discovery and a live grant to share the connection's
    profile hash; HTTP rows require null identities. Disabling bumps the revision and clears the
    check identity so previously reviewed discovery/grants cannot authorize a later enable.
    The caller owns the transaction and commit.
    """
    row = await get_connection(session, owner_id, connection_id, lock=True)
    if row.revision != expected_revision:
        raise McpConflict("Connection revision conflict")
    if enabled:
        if row.health_code != "connected":
            raise ValueError("Connection needs a successful real server-owned draft check")
        if row.transport == "stdio":
            if (row.deployment_profile_hash is None
                    or row.draft_check_profile_hash != row.deployment_profile_hash):
                raise ValueError("stdio connection needs a successful check for its current profile")
        elif row.deployment_profile_hash is not None or row.draft_check_profile_hash is not None:
            raise ValueError("HTTP connection has an invalid deployment profile identity")
        discovery = await session.scalar(select(McpDiscovery).where(
            McpDiscovery.connection_id == connection_id,
            McpDiscovery.connection_revision == row.revision,
        ).order_by(McpDiscovery.created_at.desc(), McpDiscovery.id).limit(1)
            .execution_options(populate_existing=True))
        if discovery is None:
            raise ValueError("Connection needs a current discovery before enable")
        if discovery.deployment_profile_hash != row.deployment_profile_hash:
            raise ValueError("Connection needs discovery for its current deployment profile")
        active_grant = await session.scalar(select(McpCapabilityGrant.id).join(
            McpCapability, McpCapability.id == McpCapabilityGrant.capability_id
        ).where(
            McpCapabilityGrant.connection_id == connection_id,
            McpCapabilityGrant.reviewed_connection_revision == row.revision,
            McpCapabilityGrant.revoked_at.is_(None),
            (McpCapabilityGrant.expires_at.is_(None) |
             (McpCapabilityGrant.expires_at > datetime.now(UTC))),
            McpCapabilityGrant.descriptor_hash == McpCapability.descriptor_hash,
            McpCapabilityGrant.reviewed_profile_hash == row.deployment_profile_hash,
            McpCapability.discovery_id == discovery.id,
        ).limit(1).execution_options(populate_existing=True))
        if active_grant is None:
            raise ValueError("Connection needs a current reviewed capability grant before enable")
    row.enabled = enabled
    if not enabled:
        row.revision += 1
    row.updated_at = datetime.now(UTC)
    if not enabled:
        row.draft_check_profile_hash = None
        row.health_code = "disabled"
        row.health_at = row.updated_at
    await session.flush()
    return to_connection_read(row)


async def record_draft_check(
    session: AsyncSession, owner_id: int, connection_id: UUID, expected_revision: int, *,
    result_code: str, captured_profile_hash: str | None = None,
) -> ConnectionRead:
    """Persist a real completed outcome only for the captured revision and exact transport identity.

    Stdio records the profile hash only for connected; every failure clears it. HTTP requires null
    profile identities. The client calls this only after SDK negotiation and verified transport
    teardown; row locking prevents a late result from binding a revised connection.
    """
    if result_code not in {"connected", "unavailable", "protocol_error", "auth_error", "timeout"}:
        raise ValueError("Unsupported draft check result")
    row = await get_connection(session, owner_id, connection_id, lock=True)
    if row.revision != expected_revision:
        raise McpConflict("Connection revision conflict")
    if row.transport == "stdio":
        if row.deployment_profile_hash is None or captured_profile_hash != row.deployment_profile_hash:
            raise McpConflict("stdio deployment profile changed during draft check")
    elif row.deployment_profile_hash is not None or captured_profile_hash is not None:
        raise McpConflict("HTTP draft check cannot bind a stdio profile")
    row.draft_check_profile_hash = captured_profile_hash if result_code == "connected" else None
    row.health_code = result_code
    row.health_at = datetime.now(UTC)
    row.updated_at = row.health_at
    await session.flush()
    return to_connection_read(row)


async def persist_discovery(session: AsyncSession, owner_id: int, connection_id: UUID, payload: DiscoveryPersist) -> DiscoveryRead:
    """Store validated descriptors only for the current revision and successful profile identity.

    The caller supplies the hash captured from its deployment resolver, never from an owner grant
    choice. This transaction locks the connection, compares profile identity, validates descriptor
    fingerprints and measures PostgreSQL JSONB sizes before inserts.
    """
    connection = await get_connection(session, owner_id, connection_id, lock=True)
    if connection.revision != payload.connection_revision:
        raise McpConflict("Connection changed during discovery")
    if payload.deployment_profile_hash != connection.deployment_profile_hash:
        raise McpConflict("Deployment profile changed during discovery")
    if (connection.transport == "stdio" and (
            connection.deployment_profile_hash is None
            or connection.draft_check_profile_hash != connection.deployment_profile_hash
    )):
        raise McpConflict("stdio discovery requires the current successful profile check")
    identity = sorted(
        ({"kind": c.kind, "key": c.remote_key, "hash": c.descriptor_hash} for c in payload.capabilities),
        key=lambda item: (item["kind"], item["key"]),
    )
    expected_hash = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if expected_hash != payload.schema_set_hash:
        raise ValueError("Discovery schema-set hash does not match validated descriptors")
    for descriptor in payload.capabilities:
        if descriptor.kind not in {"tool", "resource", "resource_template"}:
            raise ValueError("Discovery contains an unsupported capability kind")
        canonical_descriptor = json.dumps(
            {"kind": descriptor.kind, "remote_key": descriptor.remote_key,
             "descriptor": descriptor.descriptor},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(canonical_descriptor).hexdigest() != descriptor.descriptor_hash:
            raise ValueError("Discovery descriptor hash does not match canonical descriptor")
    descriptor_array = bindparam(
        "mcp_descriptor_jsonb_values",
        value=[item.descriptor for item in payload.capabilities],
        type_=JSONB,
    )
    descriptor_rows = func.jsonb_array_elements(descriptor_array).table_valued("value").alias("descriptor_values")
    sizes = await session.execute(select(
        func.max(func.octet_length(cast(descriptor_rows.c.value, Text))),
        func.octet_length(cast(descriptor_array, Text)),
    ).select_from(descriptor_rows))
    largest_descriptor_bytes, rendered_array_bytes = sizes.one()
    if ((largest_descriptor_bytes or 0) > 65_536 or
            (rendered_array_bytes or 0) > 1_000_000):
        raise ValueError("Discovery exceeds PostgreSQL JSONB storage byte limits")
    row = McpDiscovery(id=uuid4(), connection_id=connection_id,
                       connection_revision=connection.revision,
                       negotiated_protocol=payload.protocol,
                       server_info=payload.server_info,
                       schema_set_hash=payload.schema_set_hash,
                       deployment_profile_hash=payload.deployment_profile_hash,
                       capability_count=len(payload.capabilities))
    session.add(row)
    await session.flush()
    items = []
    for descriptor in payload.capabilities:
        items.append(McpCapability(
            id=uuid4(), discovery_id=row.id, kind=descriptor.kind,
            remote_key=descriptor.remote_key, descriptor=descriptor.descriptor,
            descriptor_hash=descriptor.descriptor_hash))
    session.add_all(items)
    await session.flush()
    connection.updated_at = datetime.now(UTC)
    return to_discovery_read(row, items)


async def get_discovery(session: AsyncSession, owner_id: int, connection_id: UUID, discovery_id: UUID) -> DiscoveryRead:
    """Read an immutable discovery only through its owner-scoped parent connection."""
    connection = await get_connection(session, owner_id, connection_id)
    row = await session.scalar(select(McpDiscovery).where(
        McpDiscovery.id == discovery_id, McpDiscovery.connection_id == connection.id))
    if row is None:
        raise McpNotFound("MCP discovery not found")
    items = (await session.scalars(select(McpCapability).where(
        McpCapability.discovery_id == row.id).order_by(McpCapability.kind, McpCapability.remote_key))).all()
    return to_discovery_read(row, list(items))


async def replace_connection_grants(session: AsyncSession, owner_id: int, connection_id: UUID, expected_revision: int, discovery_id: UUID, selections: tuple[GrantChoice, ...]) -> tuple[GrantRead, ...]:
    """Atomically replace exact descriptor grants and copy the selected discovery's profile hash.

    The connection row is locked before the current discovery and capability checks; for stdio the
    connection, successful check and selected discovery hashes must agree. The owner cannot supply
    the grant profile identity. Disabling or revision changes leave old grants stale.
    """
    connection = await get_connection(session, owner_id, connection_id, lock=True)
    if connection.revision != expected_revision:
        raise McpConflict("Connection revision changed")
    discovery = await session.scalar(select(McpDiscovery).where(
        McpDiscovery.id == discovery_id, McpDiscovery.connection_id == connection_id,
        McpDiscovery.connection_revision == connection.revision).execution_options(populate_existing=True))
    if discovery is None:
        raise McpConflict("Discovery is stale or belongs to another connection")
    if (connection.transport == "stdio" and (
            connection.deployment_profile_hash is None
            or connection.draft_check_profile_hash != connection.deployment_profile_hash
            or discovery.deployment_profile_hash != connection.deployment_profile_hash
    )) or (connection.transport == "streamable_http" and (
            connection.deployment_profile_hash is not None
            or connection.draft_check_profile_hash is not None
            or discovery.deployment_profile_hash is not None
    )):
        raise McpConflict("Connection and discovery deployment identities do not match")
    if len({item.capability_id for item in selections}) != len(selections):
        raise ValueError("Capability selections must be unique")
    ids = [item.capability_id for item in selections]
    rows = (await session.scalars(select(McpCapability).where(
        McpCapability.id.in_(ids), McpCapability.discovery_id == discovery.id).with_for_update()
        .execution_options(populate_existing=True))).all() if ids else []
    by_id = {item.id: item for item in rows}
    if len(by_id) != len(selections) or any(by_id[item.capability_id].descriptor_hash != item.descriptor_hash for item in selections):
        raise McpConflict("Selection does not match exact discovered descriptors")
    now = datetime.now(UTC)
    old = (await session.scalars(select(McpCapabilityGrant).where(
        McpCapabilityGrant.connection_id == connection_id,
        McpCapabilityGrant.revoked_at.is_(None)).with_for_update()
        .execution_options(populate_existing=True))).all()
    for grant in old:
        grant.revoked_at = now
        grant.grant_revision += 1
    added = []
    for choice in selections:
        added.append(McpCapabilityGrant(
            id=uuid4(), connection_id=connection_id, capability_id=choice.capability_id,
            descriptor_hash=choice.descriptor_hash, reviewed_connection_revision=connection.revision,
            reviewed_profile_hash=discovery.deployment_profile_hash,
            grant_revision=1, purpose=choice.purpose, risk=choice.risk.value,
            source_ids=[str(value) for value in choice.source_ids],
            destinations=list(choice.destinations), expires_at=choice.expires_at))
    session.add_all(added)
    await session.flush()
    return tuple(to_grant_read(item) for item in added)


async def list_connection_grants(session: AsyncSession, owner_id: int, connection_id: UUID) -> tuple[GrantRead, ...]:
    """Return owner-scoped grant metadata, including revoked rows for audit visibility."""
    await get_connection(session, owner_id, connection_id)
    rows = (await session.scalars(select(McpCapabilityGrant).where(
        McpCapabilityGrant.connection_id == connection_id).order_by(McpCapabilityGrant.reviewed_at.desc()))).all()
    return tuple(to_grant_read(row) for row in rows)


def to_execution_fence(connection: McpConnection, discovery: McpDiscovery, capability: McpCapability, grant: McpCapabilityGrant, destination_id: str) -> ExecutionFence:
    """Derive a detached fence only when connection, check, discovery and grant identity chains agree."""
    if connection.transport == "stdio":
        if (connection.deployment_profile_hash is None
                or connection.draft_check_profile_hash != connection.deployment_profile_hash
                or discovery.deployment_profile_hash != connection.deployment_profile_hash
                or grant.reviewed_profile_hash != connection.deployment_profile_hash):
            raise McpConflict("stdio profile identity requires renewed review")
    elif (connection.deployment_profile_hash is not None or connection.draft_check_profile_hash is not None
            or discovery.deployment_profile_hash is not None or grant.reviewed_profile_hash is not None):
        raise McpConflict("HTTP rows cannot carry a stdio profile identity")
    if not connection.enabled or connection.revision != grant.reviewed_connection_revision or discovery.connection_revision != connection.revision:
        raise McpConflict("Connection or discovery requires renewed review")
    if grant.revoked_at is not None or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)):
        raise McpConflict("Capability grant is expired or revoked")
    if capability.descriptor_hash != grant.descriptor_hash or grant.descriptor_hash != capability.descriptor_hash:
        raise McpConflict("Capability descriptor changed after review")
    if destination_id not in grant.destinations:
        raise McpConflict("Destination is outside the reviewed grant")
    return ExecutionFence(
        connection_id=connection.id, connection_revision=connection.revision,
        deployment_profile_hash=connection.deployment_profile_hash,
        grant_id=grant.id, grant_revision=grant.grant_revision,
        discovery_id=discovery.id, descriptor_hash=capability.descriptor_hash,
        remote_capability_key=capability.remote_key, purpose=grant.purpose,
        source_ids=tuple(UUID(value) for value in grant.source_ids),
        destination_id=destination_id, timeout_seconds=connection.timeout_seconds,
        limits={"response_bytes": 262144, "argument_bytes": 64000})


async def resolve_capability_fence(session: AsyncSession, owner_id: int, connection_id: UUID, grant_id: UUID, destination_id: str) -> ExecutionFence:
    """Resolve fresh grant state in connection-then-grant lock order; caller must end its transaction before remote I/O."""
    connection = await get_connection(session, owner_id, connection_id, lock=True)
    grant = await session.scalar(select(McpCapabilityGrant).where(
        McpCapabilityGrant.id == grant_id, McpCapabilityGrant.connection_id == connection.id).with_for_update()
        .execution_options(populate_existing=True))
    if grant is None:
        raise McpNotFound("MCP capability grant not found")
    capability = await session.scalar(select(McpCapability).where(
        McpCapability.id == grant.capability_id).execution_options(populate_existing=True))
    discovery = await session.scalar(select(McpDiscovery).where(
        McpDiscovery.id == capability.discovery_id, McpDiscovery.connection_id == connection.id
    ).execution_options(populate_existing=True)) if capability else None
    if capability is None or discovery is None:
        raise McpConflict("Capability discovery is no longer available")
    return to_execution_fence(connection, discovery, capability, grant, destination_id)


async def revalidate_capability_fence(session: AsyncSession, owner_id: int, fence: ExecutionFence, *, lock: bool = False) -> bool:
    """Refresh persisted revisions and scope in the caller transaction; false means discard and transaction must end before I/O."""
    try:
        connection = await get_connection(session, owner_id, fence.connection_id, lock=lock)
    except McpNotFound:
        return False
    grant = await session.scalar(select(McpCapabilityGrant).where(
        McpCapabilityGrant.id == fence.grant_id, McpCapabilityGrant.connection_id == connection.id
    ).execution_options(populate_existing=True))
    if grant is None:
        return False
    capability = await session.scalar(select(McpCapability).where(
        McpCapability.id == grant.capability_id).execution_options(populate_existing=True))
    discovery = await session.scalar(select(McpDiscovery).where(
        McpDiscovery.id == fence.discovery_id, McpDiscovery.connection_id == connection.id
    ).execution_options(populate_existing=True))
    if capability is None or discovery is None:
        return False
    try:
        current = to_execution_fence(connection, discovery, capability, grant, fence.destination_id)
    except McpConflict:
        return False
    return current == fence


async def create_inbound_client(session: AsyncSession, owner_id: int, data: InboundClientCreate) -> InboundClientIssued:
    """Persist only a digest and return a fresh high-entropy token once in the issuance DTO."""
    raw, digest, prefix = issue_inbound_token()
    row = McpInboundClient(
        id=uuid4(), owner_id=owner_id, name=data.name, token_hash=digest,
        token_prefix=prefix, audience=data.audience,
        bindings=[binding.model_dump(mode="json") for binding in data.tool_bindings],
        source_ids=[str(value) for value in data.source_ids],
        capabilities=list(data.capabilities), expires_at=data.expires_at, revision=1)
    session.add(row)
    await session.flush()
    return InboundClientIssued(client=to_inbound_read(row), token=raw)


async def rotate_inbound_client(session: AsyncSession, owner_id: int, client_id: UUID, expected_revision: int) -> InboundClientIssued:
    """Refresh and lock the expected owner identity, then stage old-token revocation and replacement issuance together."""
    old = await session.scalar(select(McpInboundClient).where(
        McpInboundClient.id == client_id, McpInboundClient.owner_id == owner_id).with_for_update()
        .execution_options(populate_existing=True))
    if old is None:
        raise McpNotFound("Inbound MCP client not found")
    if old.revision != expected_revision or old.revoked_at is not None or old.expires_at <= datetime.now(UTC):
        raise McpConflict("Inbound client is changed, revoked, or expired")
    raw, digest, prefix = issue_inbound_token()
    old.revoked_at = datetime.now(UTC)
    old.revision += 1
    replacement = McpInboundClient(
        id=uuid4(), owner_id=owner_id, name=old.name, token_hash=digest,
        token_prefix=prefix, audience=old.audience, bindings=old.bindings,
        source_ids=old.source_ids, capabilities=old.capabilities,
        expires_at=old.expires_at, revision=1)
    session.add(replacement)
    await session.flush()
    return InboundClientIssued(client=to_inbound_read(replacement), token=raw)


async def list_inbound_clients(session: AsyncSession, owner_id: int) -> tuple[InboundClientRead, ...]:
    """Return metadata for every inbound client belonging to the authenticated owner."""
    rows = (await session.scalars(select(McpInboundClient).where(
        McpInboundClient.owner_id == owner_id).order_by(McpInboundClient.created_at.desc()))).all()
    return tuple(to_inbound_read(row) for row in rows)


async def revoke_inbound_client(session: AsyncSession, owner_id: int, client_id: UUID) -> InboundClientRead:
    """Stage revocation and a revision increment in the caller transaction, which the owner route commits before returning."""
    row = await session.scalar(select(McpInboundClient).where(
        McpInboundClient.id == client_id, McpInboundClient.owner_id == owner_id).with_for_update()
        .execution_options(populate_existing=True))
    if row is None:
        raise McpNotFound("Inbound MCP client not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        row.revision += 1
        await session.flush()
    return to_inbound_read(row)


async def verify_inbound_client(session: AsyncSession, raw: str, audience: str) -> InboundPrincipal:
    """Verify token digest, exact audience, expiry and revocation then return an explicitly non-owner scope."""
    if len(raw) > 512 or not raw:
        raise McpNotFound("Inbound MCP client is invalid")
    digest = hash_inbound_token(raw)
    row = await session.scalar(select(McpInboundClient).where(
        McpInboundClient.token_hash == digest).execution_options(populate_existing=True))
    if row is None or not secrets.compare_digest(digest, row.token_hash):
        raise McpNotFound("Inbound MCP client is invalid")
    if row.audience != audience or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
        raise McpNotFound("Inbound MCP client is invalid")
    return InboundPrincipal(
        client_id=row.id, owner_id=row.owner_id, audience=row.audience,
        revision=row.revision,
        bindings=tuple(InboundBinding.model_validate(item) for item in row.bindings),
        source_ids=tuple(UUID(value) for value in row.source_ids),
        capabilities=tuple(row.capabilities), destination_id=f"mcp-client:{row.id}")


async def revalidate_inbound_principal(session: AsyncSession, expected: InboundPrincipal) -> bool:
    """Refresh the owner-scoped client row and require its complete detached identity to remain exact."""
    row = await session.scalar(select(McpInboundClient).where(
        McpInboundClient.id == expected.client_id,
        McpInboundClient.owner_id == expected.owner_id,
    ).execution_options(populate_existing=True))
    if row is None or row.owner_id != expected.owner_id:
        return False
    expiry = row.expires_at
    if (not isinstance(expiry, datetime) or expiry.tzinfo is None or expiry.utcoffset() is None or
            row.revoked_at is not None or expiry <= datetime.now(UTC)):
        return False
    try:
        read = to_inbound_read(row)
        current = InboundPrincipal(
            client_id=read.id, owner_id=row.owner_id, audience=read.audience,
            revision=read.revision, bindings=read.bindings, source_ids=read.source_ids,
            capabilities=read.capabilities, destination_id=f"mcp-client:{read.id}",
        )
    except (TypeError, ValueError, AttributeError):
        return False
    # A revision is a useful fence, but malformed or same-revision scope edits must also deny.
    return current == expected


async def load_transport_connection(
    session: AsyncSession, owner_id: int, connection_id: UUID, *, encryption_key: str,
) -> tuple[ConnectionRead, str | None]:
    """Return detached owner metadata and decrypt only HTTP bearer credentials inside the transport owner.

    Stdio denies either bearer mode or retained ciphertext, including legacy rows, before any
    credential decryption. The fresh session closes before transport resolution or launch.
    """
    row = await get_connection(session, owner_id, connection_id)
    if row.transport == "stdio" and (row.auth_method != "none" or row.encrypted_credential is not None):
        raise McpUnavailable("MCP stdio connection cannot use bearer credentials")
    credential = None
    if row.auth_method == "bearer":
        if row.encrypted_credential is None:
            raise McpUnavailable("MCP connection credential is unavailable")
        credential = decrypt_connection_credential(
            row.encrypted_credential, key=encryption_key, owner_id=owner_id,
            connection_id=row.id, credential_revision=row.credential_revision,
        )
    return to_connection_read(row), credential


async def get_current_selection(
    session: AsyncSession, owner_id: int, connection_id: UUID,
) -> tuple[ConnectionRead, DiscoveryRead, tuple[GrantRead, ...]] | None:
    """Resolve the newest current-revision discovery and its live grants as detached owner data."""
    connection = await get_connection(session, owner_id, connection_id)
    discovery = await session.scalar(select(McpDiscovery).where(
        McpDiscovery.connection_id == connection.id,
        McpDiscovery.connection_revision == connection.revision,
    ).order_by(McpDiscovery.created_at.desc(), McpDiscovery.id).limit(1)
        .execution_options(populate_existing=True))
    if discovery is None:
        return None
    if connection.transport == "stdio":
        if (connection.deployment_profile_hash is None
                or connection.draft_check_profile_hash != connection.deployment_profile_hash
                or discovery.deployment_profile_hash != connection.deployment_profile_hash):
            return None
    elif (connection.deployment_profile_hash is not None or connection.draft_check_profile_hash is not None
            or discovery.deployment_profile_hash is not None):
        return None
    capabilities = (await session.scalars(select(McpCapability).where(
        McpCapability.discovery_id == discovery.id
    ).order_by(McpCapability.kind, McpCapability.remote_key)
        .execution_options(populate_existing=True))).all()
    now = datetime.now(UTC)
    grants = (await session.scalars(select(McpCapabilityGrant).join(
        McpCapability, McpCapability.id == McpCapabilityGrant.capability_id,
    ).where(
        McpCapabilityGrant.connection_id == connection.id,
        McpCapabilityGrant.reviewed_connection_revision == connection.revision,
        McpCapabilityGrant.revoked_at.is_(None),
        or_(McpCapabilityGrant.expires_at.is_(None), McpCapabilityGrant.expires_at > now),
        McpCapability.discovery_id == discovery.id,
        McpCapabilityGrant.descriptor_hash == McpCapability.descriptor_hash,
        McpCapabilityGrant.reviewed_profile_hash == connection.deployment_profile_hash,
    ).order_by(McpCapabilityGrant.reviewed_at, McpCapabilityGrant.id)
        .execution_options(populate_existing=True))).all()
    return (
        to_connection_read(connection), to_discovery_read(discovery, list(capabilities)),
        tuple(to_grant_read(grant) for grant in grants),
    )


async def get_inbound_client_read(
    session: AsyncSession, owner_id: int, client_id: UUID,
) -> InboundClientRead:
    """Read one fresh inbound client through the authenticated owner's identity boundary."""
    row = await session.scalar(select(McpInboundClient).where(
        McpInboundClient.id == client_id, McpInboundClient.owner_id == owner_id,
    ).execution_options(populate_existing=True))
    if row is None:
        raise McpNotFound("Inbound MCP client not found")
    return to_inbound_read(row)


async def list_runtime_connections(session: AsyncSession, owner_id: int) -> tuple[ConnectionRead, ...]:
    """Bound owner connection discovery before sequential refresh/hydration and reject catalogs above 100."""
    ids = (await session.scalars(select(McpConnection.id).where(
        McpConnection.owner_id == owner_id,
    ).order_by(McpConnection.updated_at.desc(), McpConnection.id).limit(101))).all()
    if len(ids) > 100:
        raise McpConflict("MCP runtime supports at most 100 connections")
    # The capped ID projection avoids an unbounded ORM load; refresh each row before detaching it.
    rows = []
    for connection_id in ids:
        rows.append(to_connection_read(await get_connection(session, owner_id, connection_id)))
    return tuple(rows)
