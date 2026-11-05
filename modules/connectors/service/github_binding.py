"""GitHub binding fences, segment validation and Source lifecycle cleanup/pause.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationFailedError,
)
from core.workspaces.schemas import AccessFence, Scope
from modules.connectors.models import (
    AgentBrowserGrant,
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorProvisioning,
    GithubOAuthGrant,
    GithubSourceHint,
    GithubWebhookCapacity,
)
from modules.sources.schemas import ConnectorSource, SourceFence

if TYPE_CHECKING:
    from modules.connectors.github.schemas import GitHubBindingFence, GitHubSegmentProof
    from modules.connectors.github.sync import GitHubValidatedSegment
from modules.connectors.service.access import (
    _connector_access,
    _connector_actor,
    _read_scoped_source,
)


def _github_binding_from_rows(
    source: ConnectorSource, provisioned: ConnectorProvisioning | None,
    grant: GithubOAuthGrant | None,
) -> "GitHubBindingFence | None":
    """Recompute the unchanged cryptographic binding from fresh admitted owner rows only."""
    from modules.connectors.github.schemas import GitHubBindingFence, project_github_source_config
    from modules.connectors.github.sync import github_scope_digest

    if (
        source.provider != "github" or source.status != "active" or provisioned is None
        or provisioned.source_generation != source.generation or not provisioned.desired_enabled
        or provisioned.state != "active" or provisioned.applied_revision != provisioned.desired_revision
    ):
        return None
    now = datetime.now(UTC)
    if (
        grant is None or grant.state != "ready" or grant.encrypted_tokens is None
        or grant.expires_at is None or grant.expires_at <= now
        or grant.source_generation != source.generation
        or grant.configuration_revision != provisioned.desired_revision
    ):
        return None
    config = project_github_source_config(source.configuration)
    flags: list[tuple[Literal["issue", "pull", "commit", "release"], bool]] = [
        ("issue", config.include_issues), ("pull", config.include_pulls),
        ("commit", config.include_commits), ("release", config.include_releases),
    ]
    resources = tuple(name for name, enabled in flags if enabled)
    scope_digest = github_scope_digest(
        str(source.id), source.generation, provisioned.desired_revision, grant.repository_id,
        grant.installation_id, grant.app_id, resources, config.github_history_days,
    )
    try:
        return GitHubBindingFence(
            source_id=source.id, source_generation=source.generation,
            connector_revision=provisioned.desired_revision, grant_operation_id=grant.operation_id,
            token_revision=grant.token_revision, repository_id=grant.repository_id,
            installation_id=grant.installation_id, app_id=grant.app_id,
            binding_revision=grant.binding_revision, resource_scope=resources,
            history_days=config.github_history_days, scope_sha256=scope_digest,
        )
    except (TypeError, ValueError):
        return None


async def get_github_binding_fence(
    session: AsyncSession, source_id: UUID, *, source_generation: int, connector_revision: int,
    lock: bool = False, scope: Scope, multi_workspace_enabled: bool,
) -> "GitHubBindingFence | None":
    """Acquire Source/provisioning/grant in order, or read their exact current scoped binding.

    Acquiring callers enter before domain locks. Callers already holding Source/provisioning
    use lock_github_binding_fence_in_uow; post-state validators use the nonlocking seam.
    Source UUID is globally unique but this owner must prove its exact workspace relationship.
    """
    from modules.connectors import provisioning

    if lock:
        source_fence, row, _slots = await provisioning.lock_connector(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    else:
        source_fence, row, _slots = await provisioning._read_connector_rows(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        )
    if source_fence is None:
        return None
    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source.generation != source_generation or row is None or row.desired_revision != connector_revision:
        return None
    query = select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id)
    if lock:
        query = query.with_for_update()
    grant = await session.scalar(query.execution_options(populate_existing=True))
    return _github_binding_from_rows(source, row, grant)


async def lock_github_binding_fence_in_uow(
    session: AsyncSession, source_id: UUID, *, source_generation: int, connector_revision: int,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> "GitHubBindingFence | None":
    """Acquire only GitHub grant after held admission/Source/provisioning and optional cleanup.

    Exact current Source/access is read nonlockingly. Source, provisioning and any prepared
    managed slots/browser/collector/Tools rows remain held through acceptance and one commit.
    No earlier acquiring wrapper, state lock, mutation, commit or provider I/O is reachable.
    """
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or source.generation != source_generation:
        return None
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    if row is None or row.desired_revision != connector_revision:
        return None
    grant = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    return _github_binding_from_rows(source, row, grant)


async def _current_github_binding_in_uow(
    session: AsyncSession, *, source_id: UUID, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence | None = None, source_fence: SourceFence | None = None,
) -> tuple[ConnectorSource | None, "GitHubBindingFence | None"]:
    """Freshly prove Source/provisioning/grant under caller-held parents without row locks."""
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None:
        return None, None
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    grant = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).execution_options(populate_existing=True))
    return source, _github_binding_from_rows(source, row, grant)


def _validated_github_segment(
    source: ConnectorSource, reserved_cursor_before: str | None,
    proof: "GitHubSegmentProof", fence: "GitHubBindingFence",
) -> "GitHubValidatedSegment":
    """Preserve typed raw-page/cursor recomputation and trusted proof error classification."""
    from modules.connectors.github.schemas import GitHubSegmentProof, project_github_source_config
    from modules.connectors.github.sync import validate_github_segment

    if source.provider != "github":
        raise ValidationFailedError("GitHub segment is not valid for this provider")
    try:
        proof = GitHubSegmentProof.model_validate(proof.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationFailedError("GitHub segment proof is malformed") from exc
    try:
        return validate_github_segment(
            fence, project_github_source_config(source.configuration),
            reserved_cursor_before, proof, collected_at=proof.collected_at,
        )
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("github_") else "github_segment_invalid"
        status = 409 if "stale" in code or "scope_exhausted" in code else 422
        raise DomainError(code, status_code=status) from exc


async def validate_github_collection_segment(
    session: AsyncSession, *, source: ConnectorSource, reserved_cursor_before: str | None,
    proof: "GitHubSegmentProof", scope: Scope, multi_workspace_enabled: bool,
) -> "GitHubValidatedSegment":
    """Acquire current scoped binding before recomputing one proof; caller owns final commit."""
    from modules.connectors.github.schemas import GitHubSegmentProof

    try:
        proof = GitHubSegmentProof.model_validate(proof.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationFailedError("GitHub segment proof is malformed") from exc
    current = await _read_scoped_source(session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if current != source:
        raise ConflictError("GitHub Source changed")
    fence = await get_github_binding_fence(
        session, source.id, source_generation=source.generation,
        connector_revision=proof.fence.connector_revision, lock=True,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if fence is None:
        raise ConflictError("GitHub grant is unavailable or requires reconnection")
    return _validated_github_segment(current, reserved_cursor_before, proof, fence)


async def validate_github_collection_segment_in_uow(
    session: AsyncSession, *, source: ConnectorSource, reserved_cursor_before: str | None,
    proof: "GitHubSegmentProof", binding_fence: "GitHubBindingFence", access_fence: AccessFence,
    source_fence: SourceFence, scope: Scope, multi_workspace_enabled: bool,
) -> "GitHubValidatedSegment":
    """Validate raw-page proof under held access/Source/provisioning/grant then ingestion state.

    Fresh nonlocking current rows must equal all captured fences and the admitted DTO. This
    acquires no locks, commits nothing and performs no I/O; Ingestion retains its original
    generation principal and owns receipt/cursor/pause/replay settlement in the same transaction.
    """
    current, fence = await _current_github_binding_in_uow(
        session, source_id=source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if current != source or fence is None or fence != binding_fence:
        raise ConflictError("GitHub collection binding changed")
    return _validated_github_segment(current, reserved_cursor_before, proof, fence)


def _validate_source_lifecycle_cleanup_rows(
    source_fence: SourceFence, row: ConnectorProvisioning | None,
    credentials: list[ConnectorManagedCredential], browser: AgentBrowserGrant | None,
    browser_owner: int | None, native: ConnectorNativeCredential | None, *, scope: Scope,
) -> None:
    """Reject foreign identities or future generations among optional lifecycle rows.

    Older retained generations are valid cleanup liabilities. A missing provisioning or
    individual credential/grant is an independent empty set, never a reason to skip other
    cleanup. Caller holds the exact Source, which also serializes row creation/absence.
    This validates metadata only; it does not lock, create rows or alter operation JSON.
    """
    if browser_owner is not None and browser_owner != _connector_actor(scope):
        raise ConflictError("Source browser grant owner changed")
    for item in [row, *credentials, browser, native]:
        if item is not None and (
            item.source_id != source_fence.id or item.source_generation > source_fence.generation
        ):
            raise ConflictError("Source cleanup row identity changed")


async def prepare_source_lifecycle_cleanup_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare optional provisioning, all sorted slots, actor browser grant, archived native.

    Caller already holds actual admission and the exact active/paused/archived Source.
    Fresh complete fences are compared without earlier locks. Every existing managed slot
    is held, including the full activation reference set beyond the three standard slots;
    absent rows stay absent. Older generations may be retired, future/foreign rows conflict.
    No DML, operation rewrite, commit or I/O occurs. Token/job/page preparation follows;
    creators must serialize on the same earlier workspace/Source to stabilize absence.
    """
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or source.status not in {"active", "paused", "archived"}:
        raise ConflictError("Source lifecycle preparation is unavailable")
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    credentials = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
    ).order_by(ConnectorManagedCredential.slot).with_for_update().execution_options(populate_existing=True)))
    browser_owner = await session.scalar(select(AgentBrowserGrant.owner_id).where(
        AgentBrowserGrant.source_id == source_id,
    ))
    if browser_owner is not None and browser_owner != _connector_actor(scope):
        raise ConflictError("Source browser grant owner changed")
    browser = await session.scalar(select(AgentBrowserGrant).where(
        AgentBrowserGrant.source_id == source_id, AgentBrowserGrant.owner_id == _connector_actor(scope),
    ).with_for_update().execution_options(populate_existing=True))
    native = await session.scalar(select(ConnectorNativeCredential).where(
        ConnectorNativeCredential.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True)) if source.status == "archived" else None
    _validate_source_lifecycle_cleanup_rows(
        source_fence, row, credentials, browser, browser_owner, native, scope=scope,
    )


async def apply_source_lifecycle_cleanup_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Continue ordinary prepared cleanup; acquire only later GitHub grant, hints, capacity.

    Caller retains exact admission/Source and prepared provisioning/all slots/browser/native,
    tokens and Tools jobs/pages after their scoped mutations. Fresh complete original access
    and current Source proof is mandatory; early rows are reread without FOR UPDATE. Missing
    rows are independent empty sets. Archived retirement uses held native and the later grant;
    provisioning preserves exact uncertain/journaled effects, browser revision advances once.
    Inactive cleanup locks all eligible hints by UUID without LIMIT, then capacity only for
    held reservations; missing/insufficient capacity conflicts rather than masking underflow.
    Terminal outcomes survive. No earlier acquiring helper, ingestion state/outbox, commit or
    provider I/O is allowed; earlier Source/workspace serialization stabilizes the row set.
    """
    from modules.connectors import provisioning

    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or source.status not in {"active", "paused", "archived"}:
        raise ConflictError("Source lifecycle cleanup is unavailable")
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    credentials = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
    ).order_by(ConnectorManagedCredential.slot).execution_options(populate_existing=True)))
    browser_owner = await session.scalar(select(AgentBrowserGrant.owner_id).where(
        AgentBrowserGrant.source_id == source_id,
    ))
    browser = await session.scalar(select(AgentBrowserGrant).where(
        AgentBrowserGrant.source_id == source_id, AgentBrowserGrant.owner_id == _connector_actor(scope),
    ).execution_options(populate_existing=True))
    native = await session.scalar(select(ConnectorNativeCredential).where(
        ConnectorNativeCredential.source_id == source_id,
    ).execution_options(populate_existing=True)) if source.status == "archived" else None
    _validate_source_lifecycle_cleanup_rows(
        source_fence, row, credentials, browser, browser_owner, native, scope=scope,
    )
    # Grant precedes every hint/capacity lock, even if provisioning is absent.
    grant = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).with_for_update().execution_options(populate_existing=True)) if source.status != "active" else None
    if grant is not None and grant.source_generation > source_fence.generation:
        raise ConflictError("Source GitHub grant generation changed")
    if source.status == "archived":
        await provisioning._clear_retired_source_credentials_held(
            session, source_id, native=native, grant=grant, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    if row is not None:
        await provisioning._apply_collection_fence(
            session, source_fence, row, {credential.slot: credential for credential in credentials},
            scope=scope, access_fence=access_fence,
        )
    if browser is not None:
        browser.enabled = False
        browser.grant_revision += 1
    if source.status != "active":
        hints = list(await session.scalars(select(GithubSourceHint).where(
            GithubSourceHint.source_id == source_id, GithubSourceHint.state.in_(_GITHUB_PAUSE_STATES),
        ).order_by(GithubSourceHint.id).with_for_update().execution_options(populate_existing=True)))
        releases = sum(bool(hint.capacity_reserved) for hint in hints)
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update().execution_options(populate_existing=True)) if releases else None
        _pause_prepared_github_hints(hints, capacity)
    await session.flush()


async def prepare_source_pause_cleanup_in_uow(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Prepare managed slots then browser grant before GitHub grant/ingestion state.

    Caller holds account/workspace/Source/provisioning. Exact active or paused Source/access are
    freshly proved without earlier locks. Missing provisioning conflicts; optional slots
    and browser grant may be absent. Preparation mutates nothing and grants no authority.
    """
    source = await _read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True)) if source is not None else None
    if source is None or source.status not in {"active", "paused"} or row is None or row.source_generation != source.generation:
        raise ConflictError("Source pause provisioning changed")
    await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source_id,
        ConnectorManagedCredential.slot.in_(("collector", "manual_trigger", "provider")),
    ).order_by(ConnectorManagedCredential.slot).with_for_update().execution_options(populate_existing=True))
    await session.scalar(select(AgentBrowserGrant).where(
        AgentBrowserGrant.source_id == source_id, AgentBrowserGrant.owner_id == _connector_actor(scope),
    ).with_for_update().execution_options(populate_existing=True))


_GITHUB_PAUSE_STATES = ("pending", "dispatched", "accepted_ingestion", "needs_attention", "capacity_deferred")


def _claim_matches_binding(claim: "GitHubHintClaim", fence: "GitHubBindingFence", source: ConnectorSource) -> bool:
    """Compare the claim's exact local Source and remote binding against fresh admitted rows."""
    return (
        claim.source_id == source.id == fence.source_id and claim.source_generation == source.generation == fence.source_generation
        and claim.connector_revision == fence.connector_revision and claim.repository_id == fence.repository_id
        and claim.installation_id == fence.installation_id and claim.binding_revision == fence.binding_revision
        and claim.resource in fence.resource_scope
    )


async def lock_github_hints_for_visibility_pause_in_uow(
    session: AsyncSession, *, claim: "GitHubHintClaim", binding_fence: "GitHubBindingFence",
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> None:
    """Lock exact claimed hint plus every current pause-eligible Source hint, then capacity.

    Caller retains Source/provisioning/grant and completed live lease/proof checks. The
    UUID-sorted union has no LIMIT; even an invalid-state exact claim is included so the
    following acknowledgement can reject it. No acknowledgement/pause/mutation occurs.
    """
    source, fence = await _current_github_binding_in_uow(
        session, source_id=claim.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or fence is None or fence != binding_fence or not _claim_matches_binding(claim, fence, source):
        raise ConflictError("GitHub pause binding changed")
    hints = list(await session.scalars(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source.id,
        or_(GithubSourceHint.id == claim.hint_id, GithubSourceHint.state.in_(_GITHUB_PAUSE_STATES)),
    ).order_by(GithubSourceHint.id).with_for_update().execution_options(populate_existing=True)))
    if not any(hint.id == claim.hint_id for hint in hints):
        raise ConflictError("GitHub pause claim disappeared")
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).with_for_update().execution_options(populate_existing=True))
    if capacity is None:
        raise ConflictError("GitHub pause capacity disappeared")


def _pause_prepared_github_hints(hints: list[GithubSourceHint], capacity: GithubWebhookCapacity | None) -> int:
    """Pause the current eligible prepared rows and release each still-owned slot exactly once."""
    current = [hint for hint in hints if hint.state in _GITHUB_PAUSE_STATES]
    releases = sum(bool(hint.capacity_reserved) for hint in current)
    if releases and (capacity is None or capacity.pending_count < releases):
        raise ConflictError("GitHub hint capacity changed")
    for hint in current:
        hint.state = "paused"
        hint.claim_token = None
        hint.claimed_revision = None
        hint.claim_expires_at = None
        hint.capacity_reserved = False
    if releases:
        assert capacity is not None  # noqa: S101  # mypy narrowing invariant
        capacity.pending_count -= releases
    return len(current)


async def apply_source_pause_cleanup_in_uow(
    session: AsyncSession, *, source_fence: SourceFence, previous_source_fence: SourceFence,
    scope: Scope, multi_workspace_enabled: bool, access_fence: AccessFence,
) -> None:
    """Apply complete cleanup after exact active G->paused G+1 or identical paused G.

    Source alone supplies the narrowly transitioned cleanup scope. Caller retains every
    earlier admission/Source/provisioning/managed/browser/grant/state/hint/capacity lock;
    this helper freshly reads prepared rows with no FOR UPDATE, earlier helper, I/O or
    commit. Post-ack states determine releases, preserving visibility_unverified and
    releasing a dirtied pending successor exactly once.
    """
    from modules.connectors import provisioning

    if (
        source_fence.id != previous_source_fence.id or source_fence.workspace_id != previous_source_fence.workspace_id
        or source_fence.workspace_id != scope.workspace_id or source_fence.local_only != previous_source_fence.local_only
        or not (
            previous_source_fence.status == "active" and source_fence.status == "paused"
            and source_fence.generation == previous_source_fence.generation + 1
            or previous_source_fence.status == "paused" and source_fence == previous_source_fence
        )
    ):
        raise ConflictError("Source pause transition changed")
    source = await _read_scoped_source(
        session, source_fence.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None:
        raise ConflictError("Paused Source disappeared")
    row = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source.id,
    ).execution_options(populate_existing=True))
    if row is None or row.source_generation != previous_source_fence.generation:
        raise ConflictError("Prepared provisioning changed")
    credentials = list(await session.scalars(select(ConnectorManagedCredential).where(
        ConnectorManagedCredential.source_id == source.id,
        ConnectorManagedCredential.slot.in_(("collector", "manual_trigger", "provider")),
    ).order_by(ConnectorManagedCredential.slot).execution_options(populate_existing=True)))
    browser = await session.scalar(select(AgentBrowserGrant).where(
        AgentBrowserGrant.source_id == source.id, AgentBrowserGrant.owner_id == _connector_actor(scope),
    ).execution_options(populate_existing=True))
    hints = list(await session.scalars(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source.id, GithubSourceHint.state.in_(_GITHUB_PAUSE_STATES),
    ).order_by(GithubSourceHint.id).execution_options(populate_existing=True)))
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).execution_options(populate_existing=True))
    if capacity is None:
        raise ConflictError("Prepared GitHub capacity disappeared")
    await provisioning._apply_collection_fence(session, source_fence, row, {credential.slot: credential for credential in credentials}, scope=scope, access_fence=access_fence)
    if browser is not None:
        browser.enabled = False
        browser.grant_revision += 1
    _pause_prepared_github_hints(hints, capacity)
    await session.flush()


async def reset_github_collection_cursor(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    expected_scope_sha256: str,
    scope: Scope, multi_workspace_enabled: bool,
) -> None:
    """Reset GitHub progress only for the currently locked binding and exact reviewed scope hash.

    Lock order is source, provisioning, GitHub grant, then ingestion state. Ingestion rejects live
    collection leases or nonterminal runs and publishes the cursor clear through its receipt owner.
    """
    import re

    from modules.connectors import provisioning
    from modules.connectors.models import GithubSyncReset
    from modules.ingestion import public as ingestion

    if re.fullmatch(r"[0-9a-f]{64}", expected_scope_sha256) is None:
        raise ValidationFailedError("GitHub scope review is invalid")
    source_fence, _row, _slots = await provisioning.lock_connector(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source_fence is None:
        raise NotFoundError("Source not found")
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    fence = await lock_github_binding_fence_in_uow(
        session, source_id, source_generation=source_generation, connector_revision=connector_revision,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    if fence is None:
        raise ConflictError("GitHub binding is unavailable")
    if fence.scope_sha256 != expected_scope_sha256:
        raise ConflictError("GitHub scope changed; review current sync status")
    session.add(GithubSyncReset(
        source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, scope_sha256=fence.scope_sha256,
    ))
    await ingestion.reset_native_collection_cursor(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence)


class GitHubHintClaim(BaseModel):
    """Carry an exact dirty revision claim into the current provider collection receipt UoW."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID
    source_generation: int
    connector_revision: int
    repository_id: str
    installation_id: str
    binding_revision: int
    hint_id: UUID
    dirty_revision: int
    resource: str
    locator_kind: str
    locator: str
    intent: str
    reconcile_page: int = Field(ge=1, le=101)
    lease_token: UUID
    expires_at: datetime
