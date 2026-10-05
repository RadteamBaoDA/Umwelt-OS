"""Owner-authenticated management API for persisted MCP connection and grant records."""

from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.dependencies import require_owner, require_owner_write
from core.auth.models import AuthSession
from core.auth.public import revalidate_owner_session
from core.config import Settings
from core.database import get_session
from modules.tools import mcp_repository as repository
from modules.tools.mcp_schemas import ConnectionDraft, ConnectionSave, GrantSelection, InboundClientCreate
from modules.settings.public import module_dependency

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp-management"], dependencies=[Depends(module_dependency("tools"))])
Session = Annotated[AsyncSession, Depends(get_session)]
OwnerRead = Annotated[AuthSession, Depends(require_owner)]
OwnerWrite = Annotated[AuthSession, Depends(require_owner_write)]


async def _revalidate_management_owner(
    session_factory: async_sessionmaker[AsyncSession],
    token_hash: str,
    owner_id: int,
) -> bool:
    """Check detached management identity in a fresh short Auth session; any DB denial fails closed.

    The callback retains only the request's token digest and owner ID, never its ORM session or
    AuthSession row. The factory context closes before the result returns to SDK request handling.
    """
    try:
        async with session_factory() as fresh_session:
            return await revalidate_owner_session(fresh_session, token_hash, owner_id)
    except Exception:
        return False


async def list_connections_route(session: Session, owner: OwnerRead) -> dict[str, object]:
    """List credential-free owner snapshots, returning a sanitized conflict for catalogs over 100.

    The read dependency supplies the authenticated owner scope; disabled saved connections count
    toward the repository bound. No ORM rows or credential material are returned to the caller.
    """
    try:
        items = await repository.list_connections(session, owner.owner_id)
    except repository.McpConflict as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="MCP connection catalog exceeds its 100-connection limit") from exc
    return {"items": [item.model_dump(mode="json") for item in items]}


async def create_connection_route(payload: ConnectionDraft, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Create an owner-scoped connection and select any stdio hash from the deployment catalog.

    The write dependency supplies owner, Origin and CSRF checks. Repository advisory locking and
    counting occur before insertion or credential encryption; this route owns commit and explicitly
    rolls back failed transactions. It passes only the admin-selected profile ID and catalog hash;
    command fields never enter this request. An unconfigured/unknown profile fails before persistence.
    Conflict detail is stable and excludes internal lock/database data.
    """
    settings: Settings = request.app.state.settings
    deployment_profile_hash = None
    if payload.transport.value == "stdio":
        try:
            deployment_profile_hash = request.app.state.mcp_runtime.profile_catalog.get_identity(
                payload.deployment_profile_id,
            )
        except repository.McpUnavailable as exc:
            raise HTTPException(status_code=503, detail="MCP stdio deployment profile is unavailable") from exc
    try:
        item = await repository.save_connection(session, owner.owner_id, None, 0, payload,
            encryption_key=settings.connector_credential_encryption_key.get_secret_value(),
            deployment_profile_hash=deployment_profile_hash)
        await session.commit()
    except repository.McpConflict as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="MCP connection catalog is full or busy; update an existing connection or retry") from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail="MCP connection input is invalid") from exc
    return item.model_dump(mode="json")


async def get_connection_route(connection_id: UUID, session: Session, owner: OwnerRead) -> dict[str, object]:
    """Read one owner-scoped connection without returning its encrypted credential."""
    try:
        row = await repository.get_connection(session, owner.owner_id, connection_id)
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    return repository.to_connection_read(row).model_dump(mode="json")


async def update_connection_route(connection_id: UUID, payload: ConnectionSave, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Commit an optimistic edit with the current server-selected profile hash, then refresh registration.

    Revisions disable the connection and clear its prior successful profile check; retained bearer
    material prevents conversion to stdio. HTTP saves always pass a null profile identity.
    """
    settings: Settings = request.app.state.settings
    deployment_profile_hash = None
    if payload.draft.transport.value == "stdio":
        try:
            deployment_profile_hash = request.app.state.mcp_runtime.profile_catalog.get_identity(
                payload.draft.deployment_profile_id,
            )
        except repository.McpUnavailable as exc:
            raise HTTPException(status_code=503, detail="MCP stdio deployment profile is unavailable") from exc
    try:
        item = await repository.save_connection(session, owner.owner_id, connection_id,
            payload.expected_revision, payload.draft,
            encryption_key=settings.connector_credential_encryption_key.get_secret_value(),
            deployment_profile_hash=deployment_profile_hash)
        await session.commit()
        try:
            await request.app.state.mcp_runtime.refresh_connection(owner.owner_id, connection_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "mcp_runtime_refresh_failed", "message": "Connection was saved but runtime refresh is unavailable"}) from exc
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="MCP connection changed; reload and retry") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MCP connection input is invalid") from exc
    return item.model_dump(mode="json")


async def draft_check_route(connection_id: UUID, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Run the SDK draft check under repeated fresh owner-session checks and return its committed snapshot.

    The owner ID and token digest are detached before rolling back the request SQL transaction. The
    per-call Auth callback guards admission wait, every provider send and outcome persistence; it is
    not stored on the shared SDK client. Revocation or expiry returns a bounded conflict without
    persisting an unauthorized draft outcome.
    """
    owner_id, token_hash = owner.owner_id, owner.token_hash
    await session.rollback()
    management_revalidator = partial(
        _revalidate_management_owner, request.app.state.session_factory, token_hash, owner_id,
    )
    try:
        item = await request.app.state.mcp_runtime.client.check_connection(
            owner_id, connection_id, management_revalidator=management_revalidator,
        )
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="MCP connection changed during draft check") from exc
    except repository.McpUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "mcp_transport_unavailable", "message": "MCP transport is unavailable"}) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "mcp_transport_unavailable", "message": "MCP draft check did not complete"}) from exc
    return item.model_dump(mode="json")


async def discover_route(connection_id: UUID, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Discover under repeated fresh owner-session checks and persist only the authorized snapshot.

    The owner ID and token digest are detached before rolling back the request SQL transaction. The
    per-call Auth callback guards admission wait, every provider send and descriptor persistence;
    the shared SDK client retains no request identity. Revocation or expiry aborts before persistence.
    """
    owner_id, token_hash = owner.owner_id, owner.token_hash
    await session.rollback()
    management_revalidator = partial(
        _revalidate_management_owner, request.app.state.session_factory, token_hash, owner_id,
    )
    try:
        item = await request.app.state.mcp_runtime.client.discover(
            owner_id, connection_id, management_revalidator=management_revalidator,
        )
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="MCP connection changed during discovery") from exc
    except repository.McpUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "mcp_transport_unavailable", "message": "MCP transport is unavailable"}) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "mcp_transport_unavailable", "message": "MCP discovery did not complete"}) from exc
    try:
        await request.app.state.mcp_runtime.refresh_connection(owner_id, connection_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "mcp_runtime_refresh_failed", "message": "Discovery was saved but runtime refresh is unavailable"}) from exc
    return item.model_dump(mode="json")


async def replace_grants_route(connection_id: UUID, payload: GrantSelection, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Commit exact owner-reviewed descriptor selections, then refresh their bounded runtime snapshot.

    The repository checks expected connection/discovery revisions and exact descriptor hashes in
    one owner write transaction. Registration refresh runs only after commit; fresh durable fences
    remain authoritative if that refresh fails. Stale selections return a bounded conflict.
    """
    try:
        items = await repository.replace_connection_grants(session, owner.owner_id, connection_id,
            payload.expected_connection_revision, payload.discovery_id, payload.selections)
        await session.commit()
        try:
            await request.app.state.mcp_runtime.refresh_connection(owner.owner_id, connection_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "mcp_runtime_refresh_failed", "message": "Grants were saved but runtime refresh is unavailable"}) from exc
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="MCP connection or discovery changed; reload and retry") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MCP grant selection is invalid") from exc
    return {"items": [item.model_dump(mode="json") for item in items]}


async def list_grants_route(connection_id: UUID, session: Session, owner: OwnerRead) -> dict[str, object]:
    """List owner-scoped current and revoked grant metadata for audit and review."""
    try:
        items = await repository.list_connection_grants(session, owner.owner_id, connection_id)
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    return {"items": [item.model_dump(mode="json") for item in items]}


async def enable_connection_route(connection_id: UUID, expected_revision: int, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Enable a current draft-checked connection and refresh its registration only after commit."""
    try:
        item = await repository.set_connection_enabled(session, owner.owner_id, connection_id, expected_revision, True)
        await session.commit()
        try:
            await request.app.state.mcp_runtime.refresh_connection(owner.owner_id, connection_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "mcp_runtime_refresh_failed", "message": "Connection was enabled but runtime refresh is unavailable"}) from exc
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="MCP connection changed; reload and retry") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MCP connection cannot be enabled") from exc
    return item.model_dump(mode="json")


async def disable_connection_route(connection_id: UUID, expected_revision: int, request: Request, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Commit the durable disable fence, then remove its detached runtime registration."""
    try:
        item = await repository.set_connection_enabled(session, owner.owner_id, connection_id, expected_revision, False)
        await session.commit()
        try:
            await request.app.state.mcp_runtime.refresh_connection(owner.owner_id, connection_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "mcp_runtime_refresh_failed", "message": "Connection was disabled but runtime refresh is unavailable"}) from exc
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="MCP connection not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="MCP connection changed; reload and retry") from exc
    return item.model_dump(mode="json")


async def list_inbound_clients_route(session: Session, owner: OwnerRead) -> dict[str, object]:
    """List metadata only for inbound clients issued by the authenticated owner."""
    return {"items": [item.model_dump(mode="json") for item in await repository.list_inbound_clients(session, owner.owner_id)]}


async def create_inbound_client_route(payload: InboundClientCreate, request: Request, response: Response, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Issue a scoped inbound token for the server audience and return its secret once after commit.

    Caller-supplied audience is replaced before repository persistence; only the digest is stored.
    Owner cookie/Origin/CSRF checks are supplied by the write dependency. The response is marked
    no-store and contains the plaintext token exactly once; validation failures expose no raw input.
    """
    payload = payload.model_copy(update={"audience": request.app.state.mcp_runtime.audience})
    try:
        result = await repository.create_inbound_client(session, owner.owner_id, payload)
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Inbound client input is invalid") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"client": result.client.model_dump(mode="json"), "token": result.token.get_secret_value()}


async def revoke_inbound_client_route(client_id: UUID, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Revoke an inbound client in its own owner scope and fence future requests by revision."""
    try:
        result = await repository.revoke_inbound_client(session, owner.owner_id, client_id)
        await session.commit()
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="Inbound MCP client not found") from exc
    return result.model_dump(mode="json")


async def rotate_inbound_client_route(client_id: UUID, expected_revision: int, request: Request, response: Response, session: Session, owner: OwnerWrite) -> dict[str, object]:
    """Rotate one current canonical-audience client under optimistic revision and return its token once.

    The owner-scoped repository locks and revokes the old revision while staging the replacement in
    the same transaction. Noncanonical legacy audience rows are rejected for explicit reissue;
    the raw replacement token is returned only after commit with cache prevention headers.
    """
    try:
        current = await repository.get_inbound_client_read(session, owner.owner_id, client_id)
        if current.audience != request.app.state.mcp_runtime.audience:
            raise HTTPException(status_code=409, detail="Inbound client audience requires canonical reissue")
        result = await repository.rotate_inbound_client(session, owner.owner_id, client_id, expected_revision)
        await session.commit()
    except repository.McpNotFound as exc:
        raise HTTPException(status_code=404, detail="Inbound MCP client not found") from exc
    except repository.McpConflict as exc:
        raise HTTPException(status_code=409, detail="Inbound client changed; reload and retry") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"client": result.client.model_dump(mode="json"), "token": result.token.get_secret_value()}


router.add_api_route("/connections", list_connections_route, methods=["GET"])
router.add_api_route("/connections", create_connection_route, methods=["POST"])
router.add_api_route("/connections/{connection_id}", get_connection_route, methods=["GET"])
router.add_api_route("/connections/{connection_id}", update_connection_route, methods=["PATCH"])
router.add_api_route("/connections/{connection_id}/draft-check", draft_check_route, methods=["POST"])
router.add_api_route("/connections/{connection_id}/discover", discover_route, methods=["POST"])
router.add_api_route("/connections/{connection_id}/grants", replace_grants_route, methods=["PUT"])
router.add_api_route("/connections/{connection_id}/grants", list_grants_route, methods=["GET"])
router.add_api_route("/connections/{connection_id}/enable", enable_connection_route, methods=["POST"])
router.add_api_route("/connections/{connection_id}/disable", disable_connection_route, methods=["POST"])
router.add_api_route("/inbound-clients", list_inbound_clients_route, methods=["GET"])
router.add_api_route("/inbound-clients", create_inbound_client_route, methods=["POST"])
router.add_api_route("/inbound-clients/{client_id}/revoke", revoke_inbound_client_route, methods=["POST"])
router.add_api_route("/inbound-clients/{client_id}/rotate", rotate_inbound_client_route, methods=["POST"])
