"""GitHub webhook delivery persistence and hint fanout/claim/ack operations.

Internal implementation behind ``modules.connectors.public`` (the stable facade).
"""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    BadRequestError,
    ConflictError,
    UnavailableError,
    ValidationFailedError,
)
from core.ids import uuid7
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope
from modules.connectors.models import (
    ConnectorProvisioning,
    GithubOAuthGrant,
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.sources.public import ConnectorSource, SourceFence

if TYPE_CHECKING:
    from modules.connectors.github.webhooks import (
        GitHubTargetHint,
        GitHubWebhookReceipt,
        VerifiedGitHubDelivery,
    )
from modules.connectors.service.access import (
    _connector_access,
    _connector_actor,
    _read_scoped_source,
)
from modules.connectors.service.github_binding import (
    _GITHUB_PAUSE_STATES,
    GitHubHintClaim,
    _claim_matches_binding,
    _current_github_binding_in_uow,
    _pause_prepared_github_hints,
    lock_github_binding_fence_in_uow,
)


class GitHubEventBinding(BaseModel):
    """Project one current or historically fenced App binding without its encrypted token."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    actor_user_id: StrictInt = Field(gt=0)
    membership_revision: StrictInt = Field(gt=0)
    workspace_configuration_revision: StrictInt = Field(gt=0)
    source_id: UUID
    source_status: str
    source_generation: int
    connector_revision: int
    grant_state: str
    grant_expires_at: datetime | None
    repository_id: str
    installation_id: str | None
    app_id: str
    binding_revision: int
    resource_scope: tuple[str, ...]


class GitHubEventBindingPage(BaseModel):
    """Return one bounded keyset page of safe webhook binding identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[GitHubEventBinding, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=512)


class _GitHubFanoutBinding(BaseModel):
    """Persist only the bounded, detached, nonsecret binding fields frozen for one fanout page."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    actor_user_id: StrictInt = Field(gt=0)
    membership_revision: StrictInt = Field(gt=0)
    workspace_configuration_revision: StrictInt = Field(gt=0)
    source_id: UUID
    source_status: Literal["active", "paused", "archived"]
    source_generation: StrictInt = Field(gt=0)
    connector_revision: StrictInt = Field(ge=0)
    grant_state: Literal["ready", "refreshing", "reconciliation_required", "revoked"]
    grant_expires_at: datetime | None
    repository_id: str = Field(pattern=r"^[0-9]{1,20}$")
    installation_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    app_id: str = Field(pattern=r"^[0-9]{1,20}$")
    binding_revision: StrictInt = Field(gt=0)
    resource_scope: tuple[Literal["issue", "pull", "commit", "release"], ...] = Field(max_length=4)

    @field_validator("grant_expires_at")
    @classmethod
    def aware_grant_expiry(cls, value: datetime | None) -> datetime | None:
        """Require persisted grant expiry to be an aware UTC instant when present."""
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Grant expiry must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def unique_resource_scope(self) -> "_GitHubFanoutBinding":
        """Reject repeated resource names in a frozen authority snapshot."""
        if len(self.resource_scope) != len(set(self.resource_scope)):
            raise ValueError("GitHub fanout resource scope must be unique")
        return self


class _GitHubFanoutAdmission(BaseModel):
    """Remember one delivery/source/distinct-target result within the current frozen page."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    actor_user_id: StrictInt = Field(gt=0)
    membership_revision: StrictInt = Field(gt=0)
    workspace_configuration_revision: StrictInt = Field(gt=0)
    source_id: UUID
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: "GitHubHintEnqueueResult"


class _GitHubFanoutPage(BaseModel):
    """Bound one restart-safe binding page and all first-admission results attached to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cursor_before: str | None = Field(default=None, max_length=512)
    cursor_after: str | None = Field(default=None, max_length=512)
    bindings: tuple[_GitHubFanoutBinding, ...] = Field(max_length=50)
    admissions: tuple[_GitHubFanoutAdmission, ...] = Field(max_length=5000)

    @model_validator(mode="after")
    def validate_fanout_bounds_and_uniqueness(self) -> "_GitHubFanoutPage":
        """Reject duplicate frozen identities and keep canonical persisted state under two MiB."""
        binding_ids = [item.source_id for item in self.bindings]
        admission_ids = [(item.source_id, item.target_sha256) for item in self.admissions]
        if len(binding_ids) != len(set(binding_ids)) or len(admission_ids) != len(set(admission_ids)):
            raise ValueError("GitHub fanout identities must be unique")
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("GitHub fanout page exceeds its byte bound")
        return self


_HintDisposition = Literal["pending", "coalesced", "ignored", "capacity_exhausted", "stale_binding", "stale_progress"]


class GitHubHintEnqueueResult(BaseModel):
    """Report the durable source target result without treating dispatch as ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    source_id: UUID
    hint_id: UUID | None = None
    dirty_revision: StrictInt | None = Field(default=None, gt=0)
    disposition: _HintDisposition


_GitHubFanoutAdmission.model_rebuild()


def _github_target_digest(target: BaseModel) -> str:
    """Hash the complete canonical target identity used to distinguish admissions in one delivery."""
    encoded = json.dumps(target.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _validated_github_fanout_page(value: object) -> _GitHubFanoutPage:
    """Strictly validate persisted fanout JSON before using its cursors, bindings, or admissions."""
    try:
        return _GitHubFanoutPage.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("github_fanout_page_invalid") from exc


def _append_github_fanout_admission(
    page: _GitHubFanoutPage,
    *,
    source_id: UUID,
    target_digest: str,
    result: "GitHubHintEnqueueResult",
    scope: Scope, access_fence: AccessFence,
) -> _GitHubFanoutPage:
    """Create a new validated page value with one atomic terminal admission result."""
    if any(entry.source_id == source_id and entry.target_sha256 == target_digest for entry in page.admissions):
        raise ValueError("github_fanout_admission_exists")
    return _validated_github_fanout_page({
        **page.model_dump(mode="python"),
        "admissions": (*page.admissions, _GitHubFanoutAdmission(
            workspace_id=scope.workspace_id, actor_user_id=_connector_actor(scope), membership_revision=scope.membership_revision,
            workspace_configuration_revision=access_fence.configuration_revision,
            source_id=source_id, target_sha256=target_digest, result=result,
        )),
    })


class PackagedCollectionWakeResult(BaseModel):
    """Describe an n8n wake attempt without implying provider fetch or indexing completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    source_id: UUID
    outcome: Literal["acknowledged", "deferred", "ambiguous", "unavailable"]
    next_eligible_at: datetime | None = None
    receipt_id: str | None = Field(default=None, max_length=128)
    run_id: UUID | None = None
    batch_id: UUID | None = None
    status: str | None = Field(default=None, max_length=32)


async def persist_verified_github_delivery(session: AsyncSession, delivery: "VerifiedGitHubDelivery") -> "GitHubWebhookReceipt":
    """Atomically reserve durable global capacity and store an authenticated delivery digest.

    A global counter row serializes capacity decisions before the digest and source-less outbox
    are committed. Raw payload bytes and secrets are never persisted. Exact replay returns the
    original receipt; reusing an ID for different bytes conflicts.
    """

    from modules.connectors.github.webhooks import GitHubWebhookReceipt, VerifiedGitHubDelivery

    try:
        normalized = VerifiedGitHubDelivery.model_validate(delivery.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise BadRequestError("GitHub delivery metadata is invalid") from exc
    existing = await session.scalar(select(GithubWebhookDelivery).where(
        GithubWebhookDelivery.receiver_revision == normalized.receiver_revision,
        GithubWebhookDelivery.delivery_id == normalized.delivery_id,
    ))
    capacity = await session.scalar(select(GithubWebhookCapacity).where(GithubWebhookCapacity.id == 1).with_for_update())
    if capacity is None:
        await session.rollback()
        raise UnavailableError("GitHub webhook capacity is unavailable")
    # Recheck after acquiring the one durable reservation lock so concurrent duplicates serialize.
    existing = await session.scalar(select(GithubWebhookDelivery).where(
        GithubWebhookDelivery.receiver_revision == normalized.receiver_revision,
        GithubWebhookDelivery.delivery_id == normalized.delivery_id,
    ))
    if existing is not None:
        existing_raw_sha256 = existing.raw_sha256
        existing_id = existing.id
        existing_received_at = existing.received_at
        await session.rollback()
        if existing_raw_sha256 != normalized.raw_sha256:
            raise ConflictError("GitHub delivery ID was reused with different bytes")
        return GitHubWebhookReceipt(
            delivery_id=normalized.delivery_id, receipt_id=existing_id, disposition="duplicate",
            accepted_at=existing_received_at,
        )
    pending = normalized.disposition == "received" and bool(normalized.targets)
    if capacity.digest_count >= 100_000 or pending and capacity.pending_count >= 100_000:
        await session.rollback()
        raise UnavailableError("GitHub webhook capacity is exhausted")
    receipt_id = uuid7()
    received_at = normalized.received_at
    session.add(GithubWebhookDelivery(
        id=receipt_id, receiver_revision=normalized.receiver_revision,
        delivery_id=normalized.delivery_id, raw_sha256=normalized.raw_sha256,
        event=normalized.event, action=normalized.action, app_id=normalized.app_id,
        installation_id=normalized.installation_id, repository_id=normalized.repository_id,
        targets=[target.model_dump(mode="json") for target in normalized.targets],
        disposition=normalized.disposition, received_at=received_at,
        detail_expires_at=received_at + timedelta(days=30),
    ))
    if pending:
        session.add(GithubWebhookOutbox(
            id=uuid7(), delivery_id=receipt_id, binding_cursor=None,
            state="pending", attempts=0, next_attempt_at=received_at,
        ))
        capacity.pending_count += 1
    capacity.digest_count += 1
    await session.commit()
    return GitHubWebhookReceipt(
        delivery_id=normalized.delivery_id, receipt_id=receipt_id,
        disposition=normalized.disposition, accepted_at=received_at,
    )


async def list_github_event_bindings(
    session: AsyncSession,
    *,
    app_id: str,
    installation_id: str,
    repository_id: str | None,
    limit: int = 50,
    cursor: str | None = None,
    multi_workspace_enabled: bool, instance_operator: bool,
) -> GitHubEventBindingPage:
    """Page all matching historical or current installation bindings without returning token data.

    Opaque cursors bind their keyset position to the exact App, installation and optional
    repository query. Paused, expired and revoked grants stay visible for access-loss fencing.
    Discovery uses only nonlocking identity/owner reads, never accumulates multiple tenant
    admission lock sets. Enqueue independently admits each captured binding before effects;
    caller owns this read transaction and its release, without hidden rollback or commit.
    """
    from modules.connectors.provisioning import activation_status
    from modules.sources import public as sources

    if instance_operator is not True or type(multi_workspace_enabled) is not bool:
        raise PermissionError("Instance binding enumeration requires configured operator admission")
    if isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("GitHub binding page limit must be between 1 and 50")
    after: UUID | None = None
    if cursor is not None:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if decoded.get("app") != app_id or decoded.get("installation") != installation_id or decoded.get("repository") != repository_id:
                raise ValueError
            after = UUID(decoded["after"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub binding cursor is invalid") from exc
    query = select(GithubOAuthGrant.source_id).where(
        GithubOAuthGrant.app_id == app_id,
        GithubOAuthGrant.installation_id == installation_id,
    )
    if repository_id is not None:
        query = query.where(GithubOAuthGrant.repository_id == repository_id)
    if after is not None:
        query = query.where(GithubOAuthGrant.source_id > after)
    grant_ids = list((await session.scalars(query.order_by(GithubOAuthGrant.source_id).limit(limit + 1))).all())
    has_more = len(grant_ids) > limit
    page = grant_ids[:limit]
    bindings: list[GitHubEventBinding] = []
    for source_id in page:
        scope = await sources.discover_source_job_identity(session, source_id, multi_workspace_enabled=multi_workspace_enabled)
        if scope is None:
            continue
        source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        grant = await session.scalar(select(GithubOAuthGrant).where(GithubOAuthGrant.source_id == source_id).execution_options(populate_existing=True)) if source is not None else None
        if source is None or grant is None:
            continue
        access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        provisioned = await activation_status(session, grant.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        revision = provisioned.desired_revision if provisioned is not None else grant.configuration_revision
        try:
            from modules.connectors.github.schemas import project_github_source_config
            config = project_github_source_config(source.configuration)
            resources = tuple(name for name, enabled in (
                ("issue", config.include_issues), ("pull", config.include_pulls),
                ("commit", config.include_commits), ("release", config.include_releases),
            ) if enabled)
            bindings.append(GitHubEventBinding(
                workspace_id=scope.workspace_id, actor_user_id=scope.actor_user_id,
                membership_revision=scope.membership_revision, workspace_configuration_revision=access_fence.configuration_revision,
                source_id=source.id, source_status=source.status,
                source_generation=source.generation, connector_revision=revision,
                grant_state=grant.state, grant_expires_at=grant.expires_at,
                repository_id=grant.repository_id, installation_id=grant.installation_id,
                app_id=grant.app_id, binding_revision=grant.binding_revision,
                resource_scope=resources,
            ))
        except (TypeError, ValueError):
            continue
    next_cursor = None
    if has_more and page:
        token = json.dumps({
            "app": app_id, "installation": installation_id,
            "repository": repository_id, "after": str(page[-1]),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        next_cursor = base64.urlsafe_b64encode(token).decode().rstrip("=")
        if len(next_cursor) > 512:
            raise ValueError("GitHub binding cursor exceeds its bound")
    return GitHubEventBindingPage(items=tuple(bindings), next_cursor=next_cursor)


async def _reserve_github_hint_slot(
    session: AsyncSession,
    *,
    outbox: GithubWebhookOutbox,
) -> bool:
    """Reserve one hint slot or transfer the already locked unfinished outbox reservation.

    Call after Source, provisioning/grant, outbox, and target hint locks. This helper then locks
    only global capacity; false leaves both reservation rows unchanged.
    """
    transfer = bool(
        outbox.capacity_reserved
        and outbox.state in {"pending", "dispatched", "needs_attention"}
    )
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).with_for_update())
    if capacity is None or capacity.pending_count >= 100_000 and not transfer:
        return False
    if transfer:
        outbox.capacity_reserved = False
    else:
        capacity.pending_count += 1
    return True


def _store_github_fanout_admission(
    outbox: GithubWebhookOutbox,
    page: _GitHubFanoutPage,
    *,
    source_id: UUID,
    target_digest: str,
    result: GitHubHintEnqueueResult,
    scope: Scope, access_fence: AccessFence,
) -> None:
    """Assign a new validated page value containing one atomic terminal admission result."""
    updated = _append_github_fanout_admission(
        page, source_id=source_id, target_digest=target_digest, result=result, scope=scope, access_fence=access_fence,
    )
    outbox.fanout_page = updated.model_dump(mode="json")


@dataclass
class _HintAdmission:
    """Mutable working state of one ``enqueue_github_hint`` call after its rows are locked.

    Holds the frozen binding/target, the admitted principal fences and the locked
    Source/provisioning/grant/outbox rows so the admission phases can be separate functions that
    share the same ``session`` and transaction. It is request-local and never shared across tasks.
    """

    session: AsyncSession
    binding: GitHubEventBinding
    target: "GitHubTargetHint"
    delivery_receipt_id: UUID
    scope: Scope
    multi_workspace_enabled: bool
    access_fence: AccessFence
    source: ConnectorSource
    source_locked: SourceFence | None
    provisioned: ConnectorProvisioning | None
    grant: GithubOAuthGrant | None
    outbox: GithubWebhookOutbox
    page: _GitHubFanoutPage
    target_digest: str
    original_source_status: str

    def result(self, disposition: _HintDisposition, **extra: Any) -> GitHubHintEnqueueResult:
        """Build an enqueue result for this workspace/Source with the given disposition."""
        return GitHubHintEnqueueResult(
            workspace_id=self.scope.workspace_id, source_id=self.binding.source_id,
            disposition=disposition, **extra,
        )

    def persist_terminal(self, result: GitHubHintEnqueueResult) -> GitHubHintEnqueueResult:
        """Attach an authoritative nonretryable result to this page before its transaction commits."""
        _store_github_fanout_admission(
            self.outbox, self.page, source_id=self.binding.source_id,
            target_digest=self.target_digest, result=result, scope=self.scope, access_fence=self.access_fence,
        )
        return result

    async def finish_terminal(self, disposition: _HintDisposition) -> GitHubHintEnqueueResult:
        """Persist a terminal ``disposition`` result, commit the transaction and return the result."""
        result = self.persist_terminal(self.result(disposition))
        await self.session.commit()
        return result


def _validate_fanout_identity(
    binding: GitHubEventBinding | _GitHubFanoutBinding, target: "GitHubTargetHint", scope: Scope,
) -> tuple[GitHubEventBinding, "GitHubTargetHint"]:
    """Re-validate the detached binding/target pair and require it to match the admitted principal.

    Raises ``ValidationFailedError`` (422) for a malformed identity and ``ConflictError`` (409)
    when the frozen principal differs from ``scope``. Pure; performs no database access.
    """
    from modules.connectors.github.webhooks import GitHubTargetHint

    try:
        checked_binding = GitHubEventBinding.model_validate(binding.model_dump(mode="python"))
        checked_target = GitHubTargetHint.model_validate(target.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationFailedError("GitHub fanout identity is invalid") from exc
    if (
        checked_binding.workspace_id != scope.workspace_id or checked_binding.actor_user_id != _connector_actor(scope)
        or checked_binding.membership_revision != scope.membership_revision
        or isinstance(scope, InternalJobScope) and (
            scope.source_id != checked_binding.source_id or scope.source_generation != checked_binding.source_generation)
    ):
        raise ConflictError("GitHub fanout principal changed")
    return checked_binding, checked_target


async def _lock_hint_admission_rows(
    session: AsyncSession, *, binding: GitHubEventBinding, target: "GitHubTargetHint",
    delivery_receipt_id: UUID, expected_binding_cursor: str | None, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence,
) -> "_HintAdmission | GitHubHintEnqueueResult":
    """Lock Source, provisioning, grant and outbox rows and resolve every pre-admission outcome.

    Lock order is Source, provisioning (optionally pausing the Source for a lost-visibility
    target), grant, then outbox. Returns a ready ``_HintAdmission`` or, after a rollback, the
    stale/duplicate ``GitHubHintEnqueueResult`` the caller must return unchanged. A missing
    delivery target raises ``ConflictError`` after rollback.
    """
    from modules.sources import public as sources

    source_locked = await sources.lock_source(session, binding.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled, expected_access_fence=access_fence)
    source = await sources.get_connector_source(session, binding.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled) if source_locked is not None else None
    if source is None:
        await session.rollback()
        return GitHubHintEnqueueResult(workspace_id=scope.workspace_id, source_id=binding.source_id, disposition="stale_binding")
    provisioned = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == binding.source_id,
    ).with_for_update())
    if target.intent == "visibility_lost" and source.status in {"active", "paused"} and provisioned is not None and provisioned.source_generation == source.generation:
        assert source_locked is not None
        await sources.prepare_source_pause_for_connector_in_uow(
            session, source.id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            access_fence=access_fence, source_fence=source_locked,
        )
    grant = await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == binding.source_id,
    ).with_for_update())
    outbox = await session.scalar(select(GithubWebhookOutbox).where(
        GithubWebhookOutbox.delivery_id == delivery_receipt_id,
    ).with_for_update())
    stale = GitHubHintEnqueueResult(workspace_id=scope.workspace_id, source_id=binding.source_id, disposition="stale_progress")
    if outbox is None or outbox.state not in {"pending", "dispatched", "needs_attention"}:
        await session.rollback()
        return stale
    try:
        page = _validated_github_fanout_page(outbox.fanout_page)
    except ValueError:
        await session.rollback()
        return stale
    if (
        outbox.binding_cursor != expected_binding_cursor
        or page.cursor_before != expected_binding_cursor
        or not any(
            item.model_dump(mode="json") == binding.model_dump(mode="json")
            for item in page.bindings
        )
    ):
        await session.rollback()
        return stale

    delivery = await session.get(GithubWebhookDelivery, delivery_receipt_id)
    if delivery is None or not any(item == target.model_dump(mode="json") for item in delivery.targets):
        await session.rollback()
        raise ConflictError("GitHub delivery target is no longer current")
    target_digest = _github_target_digest(target)
    prior = next((
        entry for entry in page.admissions
        if entry.workspace_id == scope.workspace_id and entry.actor_user_id == _connector_actor(scope)
        and entry.membership_revision == scope.membership_revision
        and entry.workspace_configuration_revision == access_fence.configuration_revision and entry.source_id == binding.source_id and entry.target_sha256 == target_digest
    ), None)
    if prior is not None:
        result = prior.result.model_copy(deep=True)
        await session.rollback()
        return result
    return _HintAdmission(
        session=session, binding=binding, target=target, delivery_receipt_id=delivery_receipt_id,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
        source=source, source_locked=source_locked, provisioned=provisioned, grant=grant,
        outbox=outbox, page=page, target_digest=target_digest, original_source_status=source.status,
    )


def _hint_binding_is_current(ctx: _HintAdmission) -> bool:
    """Return whether the frozen binding still matches the locked Source, provisioning and grant rows.

    A visibility-loss target remains admissible while the grant is unusable or the Source is
    paused; every other target needs an active Source and an unexpired ready grant.
    """
    source, binding, provisioned, grant = ctx.source, ctx.binding, ctx.provisioned, ctx.grant
    access_loss = ctx.target.intent == "visibility_lost"
    return not (
        source is None or source.provider != "github"
        or source.status == "archived"
        or source.generation != binding.source_generation
        or provisioned is None or provisioned.desired_revision != binding.connector_revision
        or provisioned.source_generation != binding.source_generation
        or grant is None or grant.repository_id != binding.repository_id
        or grant.installation_id != binding.installation_id or grant.app_id != binding.app_id
        or grant.binding_revision != binding.binding_revision
        or grant.configuration_revision != binding.connector_revision
        or (not access_loss and (
            source.status != "active" or grant.state != "ready"
            or grant.expires_at is None or grant.expires_at <= datetime.now(UTC)
        ))
        or (source.status == "paused" and not access_loss)
    )


async def _admit_visibility_loss(ctx: _HintAdmission) -> GitHubHintEnqueueResult:
    """Pause the Source and record a ``visibility_unverified`` hint for a lost-visibility target.

    Locks the Source's pause-relevant hints, then global capacity, before pausing the Source in
    this unit of work. Releases the slot of an already-reserved hint it rewrites. Commits through
    ``commit_with_replay`` only when the Source was active (so subscribers see the pause);
    otherwise a plain commit. Raises ``ConflictError`` when capacity or the Source vanished.
    """
    from core.realtime import commit_with_replay, make_source_change
    from modules.sources import public as sources

    session, binding, target, scope = ctx.session, ctx.binding, ctx.target, ctx.scope
    pause_hints = list(await session.scalars(select(GithubSourceHint).where(
        GithubSourceHint.source_id == binding.source_id,
        or_(GithubSourceHint.state.in_(_GITHUB_PAUSE_STATES), (
            (GithubSourceHint.resource == target.resource) & (GithubSourceHint.locator_kind == target.locator_kind)
            & (GithubSourceHint.locator == target.locator)
        )),
    ).order_by(GithubSourceHint.id).with_for_update().execution_options(populate_existing=True)))
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).with_for_update().execution_options(populate_existing=True))
    if capacity is None:
        raise ConflictError("GitHub visibility capacity disappeared")
    visibility_hint = next((hint for hint in pause_hints if (
        hint.resource == target.resource and hint.locator_kind == target.locator_kind and hint.locator == target.locator
    )), None)
    assert ctx.source is not None and ctx.source_locked is not None
    paused = await sources.pause_source_for_connector_in_uow(
        session, binding.source_id, scope=scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
        access_fence=ctx.access_fence, source_fence=ctx.source_locked,
    )
    if paused is None:
        raise ConflictError("GitHub visibility Source changed during cleanup")
    if visibility_hint is None:
        session.add(GithubSourceHint(
            id=uuid7(), source_id=binding.source_id,
            source_generation=paused.generation,
            connector_revision=binding.connector_revision,
            repository_id=binding.repository_id, binding_revision=binding.binding_revision,
            resource=target.resource, locator_kind=target.locator_kind, locator=target.locator,
            intent="visibility_lost", dirty_revision=1,
            last_delivery_id=ctx.delivery_receipt_id, state="visibility_unverified",
            capacity_reserved=False, next_attempt_at=datetime.now(UTC),
        ))
    else:
        visibility_hint.dirty_revision += 1
        visibility_hint.source_generation = paused.generation
        visibility_hint.connector_revision = binding.connector_revision
        visibility_hint.repository_id = binding.repository_id
        visibility_hint.binding_revision = binding.binding_revision
        visibility_hint.intent = "visibility_lost"
        visibility_hint.last_delivery_id = ctx.delivery_receipt_id
        visibility_hint.state = "visibility_unverified"
        visibility_hint.claim_token = None
        visibility_hint.claimed_revision = None
        visibility_hint.claim_expires_at = None
        if visibility_hint.capacity_reserved:
            if capacity.pending_count < 1:
                raise ConflictError("GitHub visibility capacity changed")
            capacity.pending_count -= 1
            visibility_hint.capacity_reserved = False
    result = ctx.persist_terminal(ctx.result("ignored"))
    if ctx.original_source_status == "active":
        await commit_with_replay(session, [make_source_change(paused.id, paused.generation, paused.status, scope=scope)], scope=scope, multi_workspace_enabled=ctx.multi_workspace_enabled, access_fence=ctx.access_fence)
    else:
        await session.commit()
    return result


async def _coalesce_existing_hint(
    ctx: _HintAdmission, existing: GithubSourceHint, *, intent: str, reset_reconcile_page: bool,
) -> GitHubHintEnqueueResult:
    """Re-arm an already-stored hint with the new delivery under a reserved capacity slot.

    Rolls back and reports ``capacity_exhausted`` when a slot cannot be reserved; otherwise bumps
    ``dirty_revision``, stores the terminal ``coalesced`` result and commits. ``reset_reconcile_page``
    restarts a repository reconcile from page 1.
    """
    session, binding = ctx.session, ctx.binding
    if not existing.capacity_reserved and not await _reserve_github_hint_slot(session, outbox=ctx.outbox):
        await session.rollback()
        return ctx.result("capacity_exhausted")
    existing.capacity_reserved = True
    existing.dirty_revision += 1
    existing.last_delivery_id = ctx.delivery_receipt_id
    existing.intent = intent
    existing.source_generation = binding.source_generation
    existing.connector_revision = binding.connector_revision
    existing.repository_id = binding.repository_id
    existing.binding_revision = binding.binding_revision
    existing.state = "pending"
    if reset_reconcile_page:
        existing.reconcile_page = 1
    existing.next_attempt_at = datetime.now(UTC)
    result = ctx.persist_terminal(ctx.result(
        "coalesced", hint_id=existing.id, dirty_revision=existing.dirty_revision,
    ))
    await session.commit()
    return result


async def _admit_resource_hint(ctx: _HintAdmission) -> GitHubHintEnqueueResult:
    """Coalesce into the stored hint, collapse to a repository reconcile, or create a new hint.

    A Source already holding 100 live hints widens new work to one repository-level
    ``reconcile`` hint (still coalescing into an existing one). Slot reservation failure rolls
    back and reports ``capacity_exhausted``. Every successful path commits exactly once.
    """
    session, binding, target = ctx.session, ctx.binding, ctx.target
    existing = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.source_id == binding.source_id,
        GithubSourceHint.resource == target.resource,
        GithubSourceHint.locator_kind == target.locator_kind,
        GithubSourceHint.locator == target.locator,
    ).with_for_update())
    if existing is not None:
        return await _coalesce_existing_hint(ctx, existing, intent=target.intent, reset_reconcile_page=False)

    count = await session.scalar(select(func.count()).select_from(GithubSourceHint).where(
        GithubSourceHint.source_id == binding.source_id,
        GithubSourceHint.state.in_(("pending", "dispatched", "accepted_ingestion", "needs_attention", "capacity_deferred")),
    )) or 0
    locator_kind, locator, intent = target.locator_kind, target.locator, target.intent
    if count >= 100:
        locator_kind, locator, intent = "repository", binding.repository_id, "reconcile"
        existing = await session.scalar(select(GithubSourceHint).where(
            GithubSourceHint.source_id == binding.source_id,
            GithubSourceHint.resource == target.resource,
            GithubSourceHint.locator_kind == locator_kind,
            GithubSourceHint.locator == locator,
        ).with_for_update())
        if existing is not None:
            return await _coalesce_existing_hint(ctx, existing, intent="reconcile", reset_reconcile_page=True)

    if not await _reserve_github_hint_slot(session, outbox=ctx.outbox):
        await session.rollback()
        return ctx.result("capacity_exhausted")
    hint = GithubSourceHint(
        id=uuid7(), source_id=binding.source_id,
        source_generation=binding.source_generation,
        connector_revision=binding.connector_revision,
        repository_id=binding.repository_id,
        binding_revision=binding.binding_revision,
        resource=target.resource, locator_kind=locator_kind, locator=locator,
        intent=intent, dirty_revision=1, last_delivery_id=ctx.delivery_receipt_id,
        state="pending", capacity_reserved=True, next_attempt_at=datetime.now(UTC),
    )
    session.add(hint)
    result = ctx.persist_terminal(ctx.result("pending", hint_id=hint.id, dirty_revision=hint.dirty_revision))
    await session.commit()
    return result


async def enqueue_github_hint(
    session: AsyncSession,
    *,
    binding: GitHubEventBinding | _GitHubFanoutBinding,
    delivery_receipt_id: UUID,
    target: "GitHubTargetHint",
    expected_binding_cursor: str | None,
    scope: Scope, multi_workspace_enabled: bool,
) -> GitHubHintEnqueueResult:
    """Atomically admit one distinct target within a frozen delivery binding page.

    Persisted page entries deduplicate delivery/source/target combinations across interleaved
    delivery retries. First admissions revalidate current authority and commit hint, capacity
    ownership, and result together. Lock order is Source, provisioning/grant, outbox, target hint,
    then capacity; a frozen detached binding page is bookkeeping, never provider authority.
    """
    from modules.connectors.github.schemas import project_github_source_config

    binding, target = _validate_fanout_identity(binding, target, scope)
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if access_fence.configuration_revision != binding.workspace_configuration_revision:
        raise ConflictError("GitHub fanout configuration changed")
    admitted = await _lock_hint_admission_rows(
        session, binding=binding, target=target, delivery_receipt_id=delivery_receipt_id,
        expected_binding_cursor=expected_binding_cursor, scope=scope,
        multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence,
    )
    if isinstance(admitted, GitHubHintEnqueueResult):
        return admitted
    ctx = admitted
    if not _hint_binding_is_current(ctx):
        return await ctx.finish_terminal("stale_binding")
    try:
        config = project_github_source_config(ctx.source.configuration)
    except (TypeError, ValueError):
        return await ctx.finish_terminal("stale_binding")
    enabled_resources = {
        name for name, enabled in (
            ("issue", config.include_issues), ("pull", config.include_pulls),
            ("commit", config.include_commits), ("release", config.include_releases),
        ) if enabled
    }
    if target.intent not in {"visibility_lost", "visibility_check"} and target.resource not in enabled_resources:
        return await ctx.finish_terminal("ignored")
    if target.intent == "visibility_lost":
        return await _admit_visibility_loss(ctx)
    return await _admit_resource_hint(ctx)


async def claim_github_hint(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    connector_revision: int,
    now: datetime | None = None,
    scope: Scope, multi_workspace_enabled: bool,
) -> GitHubHintClaim | None:
    """Commit one exact due hint claim after collection reservation and current grant fencing.

    Claiming never authorizes the provider read. The caller must reserve the normal ingestion
    collection lease first; receipt acceptance independently verifies that exact lease.
    Lock order is Source, provisioning, grant, then hint.
    """

    now = (now or datetime.now(UTC)).astimezone(UTC)
    from modules.connectors import provisioning

    source_fence, _row, _slots = await provisioning.lock_connector(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source_fence is None:
        await session.rollback()
        return None
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    fence = await lock_github_binding_fence_in_uow(
        session, source_id, source_generation=source_generation, connector_revision=connector_revision,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    if fence is None:
        await session.rollback()
        return None
    hint = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source_id,
        GithubSourceHint.source_generation == source_generation,
        GithubSourceHint.connector_revision == connector_revision,
        GithubSourceHint.state.in_(("pending", "dispatched", "needs_attention", "capacity_deferred")),
        or_(GithubSourceHint.capacity_reserved.is_(True), GithubSourceHint.state == "capacity_deferred"),
        GithubSourceHint.reconcile_page <= 100,
        GithubSourceHint.next_attempt_at <= now,
        (GithubSourceHint.claim_token.is_(None) | (GithubSourceHint.claim_expires_at <= now)),
    ).order_by(GithubSourceHint.updated_at, GithubSourceHint.id).limit(1).with_for_update())
    if hint is None:
        await session.rollback()
        return None
    if hint.state == "capacity_deferred" and not hint.capacity_reserved:
        if (
            hint.repository_id != fence.repository_id
            or hint.binding_revision != fence.binding_revision
            or hint.resource not in fence.resource_scope
        ):
            hint.state = "visibility_unverified"
            hint.next_attempt_at = now
            await session.commit()
            return None
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update())
        if capacity is None or capacity.pending_count >= 100_000:
            hint.next_attempt_at = now + timedelta(seconds=60)
            await session.commit()
            return None
        capacity.pending_count += 1
        hint.capacity_reserved = True
        hint.state = "pending"
    token = uuid7()
    expires = now + timedelta(seconds=60)
    hint.claim_token = token
    hint.claimed_revision = hint.dirty_revision
    hint.claim_expires_at = expires
    hint.state = "dispatched"
    await session.commit()
    return GitHubHintClaim(
        source_id=source_id, source_generation=source_generation,
        connector_revision=connector_revision, repository_id=fence.repository_id,
        installation_id=fence.installation_id, binding_revision=fence.binding_revision,
        hint_id=hint.id, dirty_revision=hint.dirty_revision, resource=hint.resource,
        locator_kind=hint.locator_kind, locator=hint.locator, intent=hint.intent,
        reconcile_page=hint.reconcile_page,
        lease_token=token, expires_at=expires,
    )


async def reconcile_github_source_hints_lifecycle(
    session: AsyncSession,
    *,
    source_id: UUID,
    source_generation: int,
    active: bool,
    scope: Scope, multi_workspace_enabled: bool,
) -> int:
    """Retire source hints on pause/archive and rearm them only under a current verified binding.

    The caller holds the Source lock and has updated/fenced its provisioning row. This method
    continues Source -> provisioning -> GitHub grant -> hint -> capacity order.
    Active rearm acquires provisioning/slots before grant. Inactive cleanup only rereads
    already-held provisioning, avoiding reentry after native/grant cleanup. Pausing clears
    exact claims and releases only slots owned by those hints; reactivation reserves fresh global
    capacity only after the active source/grant fence matches the source's new generation. If the
    global capacity is full, the durable capacity_deferred state retains current fences for the
    bounded worker and owner-claim reservation retries.
    """
    from modules.sources import public as sources

    source = await _read_scoped_source(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    source_fence = await sources.get_source_fence(session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    access_fence = await _connector_access(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None or source_fence is None or source.generation != source_generation or (active and source.status != "active"):
        return 0
    if active:
        return await _rearm_github_hints(
            session, source_id=source_id, source_generation=source_generation, scope=scope,
            multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
        )
    return await _retire_github_hints(session, source_id=source_id)


async def _rearm_github_hints(
    session: AsyncSession, *, source_id: UUID, source_generation: int, scope: Scope,
    multi_workspace_enabled: bool, access_fence: AccessFence, source_fence: SourceFence,
) -> int:
    """Rearm paused/deferred hints under the current verified binding; return how many became pending.

    Locks provisioning/slots, then the binding fence, hints (up to 100) and global capacity, in
    that order. Hints whose repository/binding/resource no longer match become
    ``visibility_unverified`` and release their slot; matching ones reserve capacity or are
    ``capacity_deferred`` for a minute when the global limit is reached. No commit.
    """
    from modules.connectors import provisioning

    _held_source, provisioned, _slots = await provisioning._lock_connector_rows(
        session, source_id, ("collector", "manual_trigger", "provider"),
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if provisioned is None:
        return 0
    fence = await lock_github_binding_fence_in_uow(
        session, source_id, source_generation=source_generation, connector_revision=provisioned.desired_revision,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled, access_fence=access_fence, source_fence=source_fence,
    )
    if fence is None:
        return 0
    candidates = list((await session.scalars(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source_id,
        GithubSourceHint.state.in_(("paused", "capacity_deferred")),
    ).order_by(GithubSourceHint.id).limit(100).with_for_update().execution_options(populate_existing=True))).all())
    if not candidates:
        return 0
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).with_for_update())
    if capacity is None:
        return 0
    rearmed = 0
    now = datetime.now(UTC)
    for hint in candidates:
        hint.source_generation = source_generation
        hint.connector_revision = fence.connector_revision
        hint.claim_token = None
        hint.claimed_revision = None
        hint.claim_expires_at = None
        if (
            hint.repository_id != fence.repository_id
            or hint.binding_revision != fence.binding_revision
            or hint.resource not in fence.resource_scope
        ):
            hint.state = "visibility_unverified"
            if hint.capacity_reserved:
                capacity.pending_count = max(0, capacity.pending_count - 1)
                hint.capacity_reserved = False
            continue
        hint.attempts = 0
        hint.reconcile_page = 1
        if not hint.capacity_reserved and capacity.pending_count >= 100_000:
            hint.state = "capacity_deferred"
            hint.next_attempt_at = now + timedelta(seconds=60)
            continue
        if not hint.capacity_reserved:
            capacity.pending_count += 1
            hint.capacity_reserved = True
        hint.state = "pending"
        hint.next_attempt_at = now
        rearmed += 1
    return rearmed


async def _retire_github_hints(session: AsyncSession, *, source_id: UUID) -> int:
    """Pause or retire a Source's live hints while holding the grant and capacity locks.

    Rereads the already-held provisioning row (no reentry), locks the grant, every live hint and,
    only when a hint owns a slot, global capacity, then releases those slots. Returns the number
    of hints changed; no commit.
    """
    provisioned = await session.scalar(select(ConnectorProvisioning).where(
        ConnectorProvisioning.source_id == source_id,
    ).execution_options(populate_existing=True))
    if provisioned is None:
        return 0
    await session.scalar(select(GithubOAuthGrant).where(
        GithubOAuthGrant.source_id == source_id,
    ).with_for_update())
    active_hints = list((await session.scalars(select(GithubSourceHint).where(
        GithubSourceHint.source_id == source_id,
        GithubSourceHint.state.in_(("pending", "dispatched", "accepted_ingestion", "needs_attention", "capacity_deferred")),
    ).order_by(GithubSourceHint.id).with_for_update())).all())
    capacity = None
    if any(hint.capacity_reserved for hint in active_hints):
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update().execution_options(populate_existing=True))
    return _pause_prepared_github_hints(active_hints, capacity)


def _acknowledge_prepared_github_hint(
    hint: GithubSourceHint | None, capacity: GithubWebhookCapacity | None, *, claim: GitHubHintClaim,
    batch_id: UUID | None, disposition: Literal["accepted_ingestion", "completed", "ignored", "visibility_unverified"],
    reconcile_next_page: int | None,
) -> bool:
    """Validate and mutate an exact prepared claim without queries, locks, commit or I/O."""
    if disposition not in {"accepted_ingestion", "completed", "ignored", "visibility_unverified"}:
        raise ValueError("Invalid GitHub hint disposition")
    now = datetime.now(UTC)
    if (
        hint is None or hint.source_id != claim.source_id or hint.id != claim.hint_id
        or hint.claim_token is None or hint.claim_token != claim.lease_token
        or hint.claimed_revision != claim.dirty_revision
        or hint.claim_expires_at is None or hint.claim_expires_at <= now or hint.claim_expires_at != claim.expires_at
        or hint.source_generation != claim.source_generation or hint.connector_revision != claim.connector_revision
        or hint.repository_id != claim.repository_id or hint.binding_revision != claim.binding_revision
        or hint.resource != claim.resource or hint.locator_kind != claim.locator_kind or hint.locator != claim.locator
    ):
        return False
    if hint.dirty_revision != claim.dirty_revision:
        hint.claim_token = None
        hint.claimed_revision = None
        hint.claim_expires_at = None
        hint.state = "pending"
        hint.next_attempt_at = now
        return True
    if hint.reconcile_page != claim.reconcile_page or hint.intent != claim.intent:
        return False
    if claim.intent == "reconcile" and reconcile_next_page is not None and (
        reconcile_next_page != claim.reconcile_page + 1 or reconcile_next_page > 101
    ):
        return False
    release = bool(hint.capacity_reserved and (
        claim.intent == "reconcile" and reconcile_next_page is not None and reconcile_next_page > 100
        or not (claim.intent == "reconcile" and reconcile_next_page is not None)
    ))
    if release and (capacity is None or capacity.pending_count < 1):
        raise ConflictError("GitHub claim capacity changed")
    hint.claim_token = None
    hint.claimed_revision = None
    hint.claim_expires_at = None
    hint.acknowledged_batch_id = batch_id
    if claim.intent == "reconcile" and reconcile_next_page is not None:
        hint.reconcile_page = reconcile_next_page
        hint.state = "pending" if reconcile_next_page <= 100 else "needs_attention"
        hint.next_attempt_at = now + timedelta(seconds=30 if reconcile_next_page <= 100 else 3600)
    else:
        hint.state = disposition
        hint.next_attempt_at = now
    if release:
        assert capacity is not None
        capacity.pending_count -= 1
        hint.capacity_reserved = False
    return True


async def acknowledge_github_hint(
    session: AsyncSession, *, claim: GitHubHintClaim, batch_id: UUID | None,
    disposition: Literal["accepted_ingestion", "completed", "ignored", "visibility_unverified"],
    reconcile_next_page: int | None = None, scope: Scope, multi_workspace_enabled: bool,
) -> bool:
    """Acquire exact hint then capacity under caller-held Source/provisioning/GitHub grant.

    Native acceptance already holds those earlier rows before state; this continuation
    only freshly proves them nonlockingly. Standalone callers must acquire them first.
    Use held acknowledgement after complete-set visibility preparation. Flush-only and
    exact dirty-revision/reconcile behavior remain owned by the final receipt transaction.
    """
    source, fence = await _current_github_binding_in_uow(
        session, source_id=claim.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if source is None or fence is None or not _claim_matches_binding(claim, fence, source):
        return False
    hint = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.id == claim.hint_id, GithubSourceHint.source_id == source.id,
    ).with_for_update().execution_options(populate_existing=True))
    capacity = None
    if hint is not None and hint.capacity_reserved:
        capacity = await session.scalar(select(GithubWebhookCapacity).where(
            GithubWebhookCapacity.id == 1,
        ).with_for_update().execution_options(populate_existing=True))
    return _acknowledge_prepared_github_hint(
        hint, capacity, claim=claim, batch_id=batch_id, disposition=disposition,
        reconcile_next_page=reconcile_next_page,
    )


async def acknowledge_github_hint_in_uow(
    session: AsyncSession, *, claim: GitHubHintClaim, batch_id: UUID | None,
    disposition: Literal["accepted_ingestion", "completed", "ignored", "visibility_unverified"],
    reconcile_next_page: int | None = None, scope: Scope, multi_workspace_enabled: bool,
    access_fence: AccessFence, source_fence: SourceFence,
) -> bool:
    """Acknowledge one exact claim after sorted complete-hint-set and capacity preparation.

    Caller holds original admission/Source/provisioning/grant before state and every hint
    before capacity. Fresh scoped Source/binding/claim validation never reacquires rows;
    prepared rows are not authority. Mutation preserves newer dirty work for immediate
    Source-wide pause, retaining only its newer reservation until that cleanup releases it.
    """
    source, fence = await _current_github_binding_in_uow(
        session, source_id=claim.source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
        access_fence=access_fence, source_fence=source_fence,
    )
    if source is None or fence is None or not _claim_matches_binding(claim, fence, source):
        return False
    hint = await session.scalar(select(GithubSourceHint).where(
        GithubSourceHint.id == claim.hint_id, GithubSourceHint.source_id == source.id,
    ).execution_options(populate_existing=True))
    capacity = await session.scalar(select(GithubWebhookCapacity).where(
        GithubWebhookCapacity.id == 1,
    ).execution_options(populate_existing=True))
    return _acknowledge_prepared_github_hint(
        hint, capacity, claim=claim, batch_id=batch_id, disposition=disposition,
        reconcile_next_page=reconcile_next_page,
    )
