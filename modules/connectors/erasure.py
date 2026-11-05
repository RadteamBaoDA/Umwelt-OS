"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.config import Settings
from core.erasure_sql import AccountErasureOwner
from core.worker_context import worker_session_factory
from modules.connectors.credentials import N8nCredentials
from modules.connectors.models import (
    ConnectorAdmissionSlot,
    ConnectorManagedCredential,
    ConnectorProviderSend,
    ConnectorProviderTerms,
    ConnectorProvisioning,
    ConnectorQuotaDebit,
    ConnectorQuotaWindow,
    GithubOAuthGrant,
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.connectors.n8n import (
    N8nApi,
    find_workflows_by_name,
    n8n_service_identity,
    workflow_matches,
)
from modules.settings.public import n8n_http_client, resolve_n8n
from modules.sources.models import Source

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "agent_browser_grants",
    "connector_admission_slots",
    "connector_collection_requests",
    "connector_managed_credentials",
    "connector_native_credentials",
    "connector_provider_sends",
    "connector_provider_terms",
    "connector_provisioning",
    "connector_quota_debits",
    "connector_quota_windows",
    "connector_rest_credentials",
    "connector_oauth_attempts",
    "connector_oauth_grants",
    "connector_schedules",
    "connector_workspace_dispatch",
    "connector_world_credentials",
    "github_oauth_attempts",
    "github_oauth_coordinators",
    "github_oauth_grants",
    "github_oauth_operations",
    "github_source_hints",
    "github_sync_resets",
    "github_webhook_capacity",
    "github_webhook_deliveries",
    "github_webhook_outbox",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)


def _settled_remote_id(envelope: dict[str, Any], *, workflow: bool) -> str | None:
    """Require a definitive journal result for a previously dispatched remote mutation.

    A provider lookup cannot prove that an earlier request will not create/update later.
    Only the trusted original-transport journal can settle that uncertainty after account
    admission is revoked. Return its exact remote ID; unresolved work retains SQL anchors.
    """
    step = envelope.get("step") if workflow else envelope
    if not isinstance(step, dict) or step.get("state") not in {"dispatched", "unknown"}:
        return None
    result = envelope.get("retained_effect_result")
    if step.get("kind") != "lookup" and (
        not isinstance(result, dict)
        or result.get("disposition") != "retained_effect_access_revoked"
        or result.get("outcome") not in {"known_success", "known_rejection", "not_sent"}
    ):
        raise RuntimeError("Unsettled n8n transport prevents account erasure")
    remote_id = result.get("remote_id") if isinstance(result, dict) else None
    if (isinstance(result, dict) and result.get("outcome") == "known_success"
            and step.get("kind") == "create" and not remote_id):
        raise RuntimeError("Settled n8n creation requires its exact remote ID")
    if remote_id is not None and (not isinstance(remote_id, str) or not remote_id or len(remote_id) > 128):
        raise RuntimeError("Invalid n8n cleanup result ID")
    return remote_id


def _same_n8n_peer(endpoints: list[str | None], endpoint: str) -> bool:
    """Keep a shared remote ID only for its original endpoint; ambiguity stops cleanup.

    Peer settings may have changed, so current configuration cannot establish historical
    ownership. Bound the peer inventory and require persisted origin for every candidate.
    """
    if len(endpoints) > 100 or any(value is None for value in endpoints):
        raise RuntimeError("Peer n8n endpoint lineage requires operator reconciliation")
    return any(n8n_service_identity(value) == endpoint for value in endpoints if value is not None)


@dataclass
class _RemoteErasurePlan:
    """Everything one remote-erasure page must delete, captured before any network I/O.

    Built inside the authorization session, which is rolled back before the first remote call, so
    the plan is detached data only: exact workflow/credential IDs, the optional name+request used to
    find unowned-match candidates, the resolved n8n client settings and an optional GitHub token to
    revoke afterwards.
    """

    source_id: UUID
    n8n: Any
    endpoint: str
    workflow_ids: set[str]
    workflow_name: str | None
    request: object
    credential_ids: set[str]
    grant_token: str | None
    grant_identity: tuple[str, str] | None


async def _plan_grant_revoke(
    session: AsyncSession, scope: AccountErasureScope, settings: Settings, grant: GithubOAuthGrant | None,
) -> tuple[str | None, tuple[str, str] | None]:
    """Return the GitHub token/identity to revoke for a source's grant, or ``(None, None)``.

    Nothing is revoked for an already confirmed revoke or when a peer workspace still holds a
    token for the same GitHub user/app (the revoke would hit its tokens too). Raises
    ``RuntimeError`` when a needed revoke lacks original app/token proof.
    """
    if grant is None:
        return None, None
    if grant.state == "revoked" and grant.encrypted_tokens is None and grant.error_code in {None, "provider_revoked"}:
        return None, None
    peer = await session.scalar(select(GithubOAuthGrant.source_id).join(
        Source, Source.id == GithubOAuthGrant.source_id,
    ).where(
        GithubOAuthGrant.github_user_id == grant.github_user_id,
        GithubOAuthGrant.app_id == grant.app_id,
        Source.workspace_id != scope.workspace_id,
        GithubOAuthGrant.encrypted_tokens.is_not(None),
    ).limit(1))
    if peer is not None:
        return None, None
    from modules.connectors.github import oauth

    if not grant.app_id or grant.app_id != settings.github_app_id or grant.encrypted_tokens is None:
        raise RuntimeError("GitHub grant revocation requires original app/token proof")
    opened = oauth._open_token_cipher(
        settings.connector_credential_encryption_key.get_secret_value(),
        grant.encrypted_tokens, grant.source_id, grant.operation_id,
        grant.source_generation, grant.configuration_revision,
    )
    candidate = opened.get("access_token")
    if not isinstance(candidate, str) or not candidate:
        raise RuntimeError("GitHub revoke token is unavailable")
    return candidate, (grant.github_user_id, grant.app_id)


def _plan_workflow_targets(provisioning: ConnectorProvisioning | None) -> tuple[set[str], str | None, object]:
    """Collect exact workflow IDs, the lookup name and request fingerprint from a provisioning row.

    The name is dropped for an unclaimed first dispatch (prepared/blocked step with no ID or saved
    name), which never created a remote liability.
    """
    workflow_ids: set[str] = set()
    workflow_name = None
    request = None
    if provisioning is None:
        return workflow_ids, workflow_name, request
    workflow_ids.update(value for value in (
        provisioning.workflow_id, provisioning.old_workflow_id,
    ) if value)
    operation = provisioning.workflow_operation or {}
    settled_id = _settled_remote_id(operation, workflow=True)
    if settled_id is not None:
        workflow_ids.add(settled_id)
    step = operation.get("step") if isinstance(operation, dict) else None
    if isinstance(step, dict):
        target = step.get("target")
        if isinstance(target, str) and target:
            workflow_ids.add(target)
        request = step.get("request")
    workflow_name = operation.get("workflow_name") if isinstance(operation, dict) else None
    workflow_name = workflow_name or provisioning.workflow_name
    if (not workflow_ids and not provisioning.workflow_name and isinstance(step, dict)
            and step.get("state") in {"prepared", "blocked"}):
        # An unclaimed first dispatch has never created a remote liability.
        workflow_name = None
    return workflow_ids, workflow_name, request


def _plan_credential_ids(credentials: list[ConnectorManagedCredential]) -> set[str]:
    """Collect every exact n8n credential ID referenced by saved slots and their operations."""
    credential_ids: set[str] = set()
    for row in credentials:
        if row.credential_id:
            credential_ids.add(row.credential_id)
        envelope = row.operation_envelope or {}
        settled_id = _settled_remote_id(envelope, workflow=False)
        if settled_id is not None:
            credential_ids.add(settled_id)
        target = envelope.get("target_id") if isinstance(envelope, dict) else None
        if isinstance(target, str) and target:
            credential_ids.add(target)
    return credential_ids


def _require_endpoint_lineage(
    provisioning: ConnectorProvisioning | None, credentials: list[ConnectorManagedCredential],
    endpoint: str, *, has_workflows: bool,
) -> None:
    """Fail closed unless every remote liability was created at the current n8n endpoint.

    Mutable current settings never establish original ownership, so the persisted origin of each
    workflow/credential must equal ``endpoint``; otherwise ``RuntimeError`` asks for operator
    reconciliation.
    """
    if has_workflows:
        original_endpoint = getattr(provisioning, "n8n_service_url", None)
        if not original_endpoint or n8n_service_identity(original_endpoint) != endpoint:
            raise RuntimeError("n8n workflow endpoint lineage requires operator reconciliation")
    for row in credentials:
        envelope = row.operation_envelope or {}
        has_liability = bool(row.credential_id or envelope.get("target_id") or _settled_remote_id(envelope, workflow=False))
        if has_liability and (not row.n8n_service_url or n8n_service_identity(row.n8n_service_url) != endpoint):
            raise RuntimeError("n8n credential endpoint lineage requires operator reconciliation")


async def _plan_remote_erasure(
    session: AsyncSession, scope: AccountErasureScope, settings: Settings, after: UUID | None,
) -> _RemoteErasurePlan | None:
    """Authorize erasure and read the next source's remote liabilities into a detached plan.

    Returns ``None`` when no source after the checkpoint remains. All reads happen in the caller's
    session, which the caller rolls back before any network call.
    """
    await authorize_account_erasure(session, scope)
    source_query = select(Source.id).where(Source.workspace_id == scope.workspace_id)
    if after is not None:
        source_query = source_query.where(Source.id > after)
    source_id = await session.scalar(source_query.order_by(Source.id).limit(1))
    if source_id is None:
        return None
    provisioning = await session.get(ConnectorProvisioning, source_id)
    credentials = list((await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
    ))).all())
    grant = await session.get(GithubOAuthGrant, source_id)
    grant_token, grant_identity = await _plan_grant_revoke(session, scope, settings, grant)
    workflow_ids, workflow_name, request = _plan_workflow_targets(provisioning)
    credential_ids = _plan_credential_ids(credentials)
    n8n = await resolve_n8n(session, settings, workspace_id=scope.workspace_id)
    endpoint = n8n_service_identity(n8n.service_url)
    _require_endpoint_lineage(
        provisioning, credentials, endpoint, has_workflows=bool(workflow_ids or workflow_name),
    )
    await session.rollback()
    return _RemoteErasurePlan(
        source_id=source_id, n8n=n8n, endpoint=endpoint, workflow_ids=workflow_ids,
        workflow_name=workflow_name, request=request, credential_ids=credential_ids,
        grant_token=grant_token, grant_identity=grant_identity,
    )


async def _delete_remote_n8n_objects(
    factory: async_sessionmaker[AsyncSession], settings: Settings, scope: AccountErasureScope,
    plan: _RemoteErasurePlan,
) -> None:
    """Delete the plan's n8n workflows and credentials, skipping those a peer endpoint still uses.

    Name lookups must match the packaged request fingerprint (``RuntimeError`` for an unowned
    match). Each ID re-checks peer references in a short session before its network call; a 404
    counts as already deleted so a crashed page can be retried safely.
    """
    n8n, endpoint = plan.n8n, plan.endpoint
    if not n8n.api_key:
        raise RuntimeError("n8n cleanup requires its API key")
    api = N8nApi(n8n.service_url, n8n.api_key, settings)
    if plan.workflow_name and isinstance(plan.request, dict):
        matches = await find_workflows_by_name(api, plan.workflow_name)
        for match in matches:
            identifier = match.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise RuntimeError("n8n workflow lookup omitted its ID")
            if not workflow_matches(plan.request, await api.get_workflow(identifier)):
                raise RuntimeError("n8n workflow name has an unowned match")
            plan.workflow_ids.add(identifier)
    for identifier in sorted(plan.workflow_ids):
        async with factory() as session:
            await authorize_account_erasure(session, scope)
            peer_endpoints = list((await session.scalars(select(ConnectorProvisioning.n8n_service_url).join(
                Source, Source.id == ConnectorProvisioning.source_id,
            ).where(Source.workspace_id != scope.workspace_id, or_(
                ConnectorProvisioning.workflow_id == identifier,
                ConnectorProvisioning.old_workflow_id == identifier,
                ConnectorProvisioning.workflow_operation["step"]["target"].astext == identifier,
                ConnectorProvisioning.workflow_operation["retained_effect_result"]["remote_id"].astext == identifier,
            )).limit(101))).all())
            await session.rollback()
        if _same_n8n_peer(peer_endpoints, endpoint):
            continue
        async with n8n_http_client(n8n.service_url, settings, timeout=20) as client:
            response = await client.delete(
                f"{n8n.service_url.rstrip('/')}/api/v1/workflows/{quote(identifier, safe='')}",
                headers={"X-N8N-API-KEY": n8n.api_key},
            )
            if response.status_code != 404:
                response.raise_for_status()
    credential_api = N8nCredentials(n8n.service_url, n8n.api_key)
    for identifier in sorted(plan.credential_ids):
        async with factory() as session:
            await authorize_account_erasure(session, scope)
            peer_endpoints = list((await session.scalars(select(ConnectorManagedCredential.n8n_service_url).join(
                Source, Source.id == ConnectorManagedCredential.source_id,
            ).where(Source.workspace_id != scope.workspace_id, or_(
                ConnectorManagedCredential.credential_id == identifier,
                ConnectorManagedCredential.operation_envelope["target_id"].astext == identifier,
                ConnectorManagedCredential.operation_envelope["retained_effect_result"]["remote_id"].astext == identifier,
            )).limit(101))).all())
            await session.rollback()
        if _same_n8n_peer(peer_endpoints, endpoint):
            continue
        await credential_api.delete(identifier)


async def _revoke_erased_github_grant(
    factory: async_sessionmaker[AsyncSession], settings: Settings, scope: AccountErasureScope,
    token: str, identity: tuple[str, str],
) -> None:
    """Revoke the GitHub grant remotely, then commit the confirmed outcome for the workspace's grants.

    GitHub revokes all tokens for this app/user, including sibling own sources, so every own
    grant for that identity is marked revoked and its ciphertext cleared. The outcome commits
    before the source cursor can be lost in a crash.
    """
    from modules.connectors.github import oauth

    await oauth.revoke_github_grant(settings, token)
    async with factory() as session:
        await authorize_account_erasure(session, scope)
        await session.execute(update(GithubOAuthGrant).where(
            GithubOAuthGrant.source_id.in_(select(Source.id).where(Source.workspace_id == scope.workspace_id)),
            GithubOAuthGrant.github_user_id == identity[0],
            GithubOAuthGrant.app_id == identity[1],
        ).values(state="revoked", encrypted_tokens=None, error_code="provider_revoked"))
        await session.commit()


async def erase_account_remote_page(
    ctx: dict[str, Any], scope: AccountErasureScope, cursor: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Delete one source's known n8n workflows/credentials before its SQL references vanish.

    Saved workflow IDs and operation targets are exact; dispatched/unknown mutations must
    have a definitive original-transport result, including its created ID. A workflow lookup
    uses the packaged request fingerprint but never proves transport cessation. Peer-referenced
    remote IDs at the same persisted endpoint survive. Legacy/unmatched origins fail closed;
    mutable current settings never establish original remote ownership. The SQL read ends
    before all network I/O and the source UUID checkpoints
    progress only after remote deletion succeeds, making 404 a safe retry result.
    """
    factory = worker_session_factory(ctx)
    settings = cast(Settings, ctx["settings"])
    after = UUID(cursor["after"]) if "after" in cursor else None
    async with factory() as session:
        plan = await _plan_remote_erasure(session, scope, settings, after)
    if plan is None:
        return True, {}
    if plan.workflow_ids or plan.credential_ids or plan.workflow_name:
        await _delete_remote_n8n_objects(factory, settings, scope, plan)
    if plan.grant_token is not None:
        assert plan.grant_identity is not None  # noqa: S101  # mypy narrowing invariant
        await _revoke_erased_github_grant(factory, settings, scope, plan.grant_token, plan.grant_identity)
    return False, {"after": str(plan.source_id)}


_SharedStep = Callable[[AsyncSession, AccountErasureScope], Awaitable[tuple[bool, dict[str, Any]]]]


async def _erase_shared_outbox(session: AsyncSession, scope: AccountErasureScope) -> tuple[bool, dict[str, Any]]:
    """Reset up to 20 outboxes whose fanout page names this workspace so peers re-fan out fresh.

    Locks the rows; stays on this step while any were reset, then moves to ``hints``.
    """
    markers = (
        {"bindings": [{"workspace_id": str(scope.workspace_id)}]},
        {"admissions": [{"workspace_id": str(scope.workspace_id)}]},
    )
    outboxes = list((await session.scalars(select(GithubWebhookOutbox).where(or_(
        *(GithubWebhookOutbox.fanout_page.contains(marker) for marker in markers)
    )).order_by(GithubWebhookOutbox.id).limit(20).with_for_update())).all())
    for row in outboxes:
        # Rebuild a peer-only page under fresh admission instead of persisting old owner DTOs.
        row.fanout_page = None
        row.binding_cursor = None
        row.state = "pending"
        row.attempts = 0
    return False, {"step": "outbox" if outboxes else "hints"}


async def _erase_shared_hints(session: AsyncSession, scope: AccountErasureScope) -> tuple[bool, dict[str, Any]]:
    """Release the capacity reservations of up to 100 of this workspace's hints.

    Raises ``RuntimeError`` if the singleton capacity row cannot cover the released count.
    """
    hints = list((await session.scalars(select(GithubSourceHint).join(
        Source, Source.id == GithubSourceHint.source_id,
    ).where(Source.workspace_id == scope.workspace_id,
            GithubSourceHint.capacity_reserved.is_(True)).limit(100).with_for_update())).all())
    if hints:
        capacity = await session.get(GithubWebhookCapacity, 1, with_for_update=True)
        if capacity is None or capacity.pending_count < len(hints):
            raise RuntimeError("GitHub webhook capacity reservation is inconsistent")
        capacity.pending_count -= len(hints)
        for hint in hints:
            hint.capacity_reserved = False
    return False, {"step": "hints" if hints else "deliveries"}


async def _erase_shared_deliveries(session: AsyncSession, scope: AccountErasureScope) -> tuple[bool, dict[str, Any]]:
    """Delete one deployment-wide delivery receipt that only this workspace's grants reference.

    A receipt is removed only when no peer workspace has a grant on its installation; its digest
    and (if reserved) pending-capacity counters are decremented in the same transaction.
    """
    grant = GithubOAuthGrant
    peer = grant.__table__.alias("peer_grant")
    own_source = Source.__table__.alias("own_source")
    peer_source = Source.__table__.alias("peer_source")
    delivery = await session.scalar(select(GithubWebhookDelivery).join(
        grant, and_(GithubWebhookDelivery.app_id == grant.app_id,
                    GithubWebhookDelivery.installation_id == grant.installation_id),
    ).join(own_source, own_source.c.id == grant.source_id).where(
        own_source.c.workspace_id == scope.workspace_id,
        grant.installation_id.is_not(None),
        ~exists(select(1).select_from(peer.join(
            peer_source, peer_source.c.id == peer.c.source_id,
        )).where(peer.c.app_id == grant.app_id,
                peer.c.installation_id == grant.installation_id,
                peer_source.c.workspace_id != scope.workspace_id)),
    ).order_by(GithubWebhookDelivery.id).limit(1).with_for_update())
    if delivery is None:
        return False, {"step": "quota"}
    outbox = await session.scalar(select(GithubWebhookOutbox).where(
        GithubWebhookOutbox.delivery_id == delivery.id,
    ).with_for_update())
    capacity = await session.get(GithubWebhookCapacity, 1, with_for_update=True)
    if capacity is None or capacity.digest_count < 1:
        raise RuntimeError("GitHub webhook capacity digest is inconsistent")
    capacity.digest_count -= 1
    if outbox is not None and outbox.capacity_reserved:
        if capacity.pending_count < 1:
            raise RuntimeError("GitHub webhook capacity pending count is inconsistent")
        capacity.pending_count -= 1
    await session.delete(delivery)
    return False, {"step": "deliveries"}


async def _erase_shared_quota(session: AsyncSession, scope: AccountErasureScope) -> tuple[bool, dict[str, Any]]:
    """Delete one of this workspace's quota debits and its window when no peer debit shares it."""
    debit = (await session.execute(select(ConnectorQuotaDebit).join(
        ConnectorProviderSend, ConnectorProviderSend.send_id == ConnectorQuotaDebit.send_id,
    ).where(ConnectorProviderSend.workspace_id == scope.workspace_id).limit(1))).scalar_one_or_none()
    if debit is None:
        return False, {"step": "slots"}
    key = (debit.provider_id, debit.budget_kind, debit.subject_hash, debit.policy_key, debit.window_start)
    await session.delete(debit)
    await session.flush()
    peer_debit = await session.scalar(select(ConnectorQuotaDebit.send_id).where(
        ConnectorQuotaDebit.provider_id == key[0], ConnectorQuotaDebit.budget_kind == key[1],
        ConnectorQuotaDebit.subject_hash == key[2], ConnectorQuotaDebit.policy_key == key[3],
        ConnectorQuotaDebit.window_start == key[4],
    ).limit(1))
    if peer_debit is None:
        await session.execute(delete(ConnectorQuotaWindow).where(
            ConnectorQuotaWindow.provider_id == key[0], ConnectorQuotaWindow.budget_kind == key[1],
            ConnectorQuotaWindow.subject_hash == key[2], ConnectorQuotaWindow.policy_key == key[3],
            ConnectorQuotaWindow.window_start == key[4],
        ))
    return False, {"step": "quota"}


async def _erase_shared_slots(session: AsyncSession, scope: AccountErasureScope) -> tuple[bool, dict[str, Any]]:
    """Free this workspace's admission slots and detach its operator review from peer terms; final step."""
    await session.execute(update(ConnectorAdmissionSlot).where(
        ConnectorAdmissionSlot.workspace_id == scope.workspace_id,
    ).values(occupied_request_id=None, workspace_id=None, admission_token=None,
             lease_kind=None, source_owner_id=None, expires_at=None))
    # A peer workspace's review stays as terms data but cannot retain this operator ID.
    await session.execute(update(ConnectorProviderTerms).where(
        ConnectorProviderTerms.workspace_id != scope.workspace_id,
        ConnectorProviderTerms.reviewer_user_id == scope.account_id,
    ).values(operator_review_state="pending", reviewer_user_id=None,
             reviewed_at=None, reviewed_allowed_use=None,
             review_evidence_ref=None, reviewed_terms_version=None,
             terms_revision=ConnectorProviderTerms.terms_revision + 1))
    return True, {}


# Checkpoint step -> handler; a new shared table adds one handler and one entry here.
_SHARED_ERASURE_STEPS: dict[str, _SharedStep] = {
    "outbox": _erase_shared_outbox,
    "hints": _erase_shared_hints,
    "deliveries": _erase_shared_deliveries,
    "quota": _erase_shared_quota,
    "slots": _erase_shared_slots,
}


async def erase_account_shared_rows(
    session: AsyncSession, scope: AccountErasureScope, cursor: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Scrub one bounded page of shared connector state while source/grant anchors remain.

    Singleton capacity rows survive with counters adjusted for removed reservations. Outbox
    pages mentioning this workspace restart fanout for peers without retaining copied owner
    snapshots. Delivery receipts are removed only when no peer grant has the installation.
    Quota windows shared by peer sends survive; exclusive windows disappear with own debits.
    The ``cursor["step"]`` checkpoint (default ``outbox``) selects the page handler from
    ``_SHARED_ERASURE_STEPS``; an unknown step raises ``ValueError``.
    """
    await authorize_account_erasure(session, scope)
    step = cursor.get("step", "outbox")
    handler = _SHARED_ERASURE_STEPS.get(step) if isinstance(step, str) else None
    if handler is None:
        raise ValueError("Invalid connector shared-erasure checkpoint")
    return await handler(session, scope)


async def verify_account_shared_rows(session: AsyncSession, scope: AccountErasureScope) -> None:
    """Reject success when any explicitly scoped shared connector slot or fanout copy survives."""
    await authorize_account_erasure(session, scope)
    if await session.scalar(select(ConnectorAdmissionSlot.slot_id).where(
        ConnectorAdmissionSlot.workspace_id == scope.workspace_id,
    ).limit(1)):
        raise RuntimeError("Connector admission slot retains account scope")
    if await session.scalar(select(GithubWebhookOutbox.id).where(or_(
        GithubWebhookOutbox.fanout_page.contains({"bindings": [{"workspace_id": str(scope.workspace_id)}]}),
        GithubWebhookOutbox.fanout_page.contains({"admissions": [{"workspace_id": str(scope.workspace_id)}]}),
    )).limit(1)):
        raise RuntimeError("GitHub fanout retains account scope")
