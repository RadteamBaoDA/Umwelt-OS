"""Per-provider proof verification for native collection acceptance (internal; import through ``public``).

``accept_native_collection`` is provider-agnostic: it owns the lock order and the persistence of the page.
Everything that depends on the provider (credential capture shape, credential/grant proofs, cursor
transition recomputation and per-record proof validation) lives behind :class:`NativeProofVerifier` and a
registry keyed by provider id, so a new provider is added by registering a verifier instead of editing the
orchestrator.

Documented lock order, enforced by the orchestrator calling the verifier hooks in this sequence::

    check_capture -> (collection fence, revision proof) -> verify_admission      # provider credential
    -> [source credentials] -> collector authorization -> verify_grant            # grant
    -> state row -> replay lookup -> reservation recheck -> verify_transition     # state / receipt
    -> verify_records -> (insert, flush) -> settle_provider_state                 # replay head last

Hooks only flush through the caller's session; none of them commits.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol
from typing import cast as typing_cast
from uuid import UUID

from sqlalchemy import select

from core.errors import (
    DomainError,
)
from core.realtime import ReplayDraft, make_source_change
from core.workspaces.schemas import AccessFence, Scope
from modules.ingestion._collection import _decode_telegram_cursor, _encode_telegram_cursor
from modules.ingestion._errors import domain_error_for_status
from modules.ingestion._native_records import validate_telegram_record_delivery
from modules.ingestion._scope import _actor_id
from modules.ingestion.models import SourceIngestionState
from modules.ingestion.schemas import (
    ConnectorCollectionLease,
    IngestionRecord,
    NativeCollectionBatch,
    TelegramDeliveryProof,
    TelegramProbeClassification,
    classify_telegram_probe,
)
from modules.sources import public as sources
from modules.sources.public import ConnectorSource, SourceFence

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from modules.connectors.public import GitHubBindingFence, GitHubSegmentProof

# A normalized row must fit the worker's bounded single-record budget.
TELEGRAM_OBSERVATION_MAX_BYTES = 4 * 1024 * 1024

RevisionPolicy = Literal["forbidden", "required", "optional"]
ProviderRecord = tuple[IngestionRecord, dict[str, object], dict[str, object] | None]


@dataclass(frozen=True)
class NativeProviderParams:
    """Provider-specific proofs the caller captured before provider I/O, as one typed object.

    The fields mirror the legacy keyword arguments one-to-one and are interpreted only by the verifier
    registered for the locked Source's provider: ``native_operation_id`` (Telegram's original native
    credential operation), ``world_credential_operation_id`` (Alpha Vantage's original world credential
    operation) and ``credential_revision`` (immutable revision proof of REST providers such as Notion,
    Slack, X and Mastodon). A shape that is inapplicable to the provider is a 422, never silently ignored.

    Design note: this is deliberately one flat frozen object rather than a union of per-provider dataclasses.
    The legacy keyword wrapper must be able to represent *any* keyword mix a caller passes (for example a
    Telegram operation id supplied for a GitHub source) so that the verifier can reject the mismatch with the
    documented 422 instead of changing behaviour at the type boundary. New call sites use the per-provider
    factories (:meth:`telegram`, :meth:`alpha_vantage`, :meth:`revisioned`), which make the intended shape
    explicit.
    """

    native_operation_id: UUID | None = None
    world_credential_operation_id: UUID | None = None
    credential_revision: int | None = None

    @classmethod
    def telegram(cls, native_operation_id: UUID) -> NativeProviderParams:
        """Build the Telegram capture (original native credential operation)."""
        return cls(native_operation_id=native_operation_id)

    @classmethod
    def alpha_vantage(cls, world_credential_operation_id: UUID) -> NativeProviderParams:
        """Build the Alpha Vantage capture (original world credential operation)."""
        return cls(world_credential_operation_id=world_credential_operation_id)

    @classmethod
    def revisioned(cls, credential_revision: int | None) -> NativeProviderParams:
        """Build a REST-provider capture carrying the provider credential revision proof."""
        return cls(credential_revision=credential_revision)


@dataclass
class NativeAcceptance:
    """Mutable state of one acceptance call shared between the orchestrator and the verifiers.

    Created by the orchestrator after the Source lock (``source``/``source_fence``/``access_fence`` are the
    locked projection and its fences); the remaining provider fields are filled by the verifier hooks. It never outlives the call and holds no locks itself.
    """

    session: AsyncSession
    payload: NativeCollectionBatch
    lease: ConnectorCollectionLease
    scope: Scope
    multi_workspace_enabled: bool
    params: NativeProviderParams
    expected_access_fence: AccessFence
    source: ConnectorSource
    source_fence: SourceFence
    access_fence: AccessFence
    bot_id: str | None = None
    github_proof: GitHubSegmentProof | None = None
    github_fence: GitHubBindingFence | None = None
    needs_visibility_fence: bool = False
    cursor_after: str | None = None
    replay_ids: set[int] = field(default_factory=set)
    accepted_proofs: dict[int, TelegramDeliveryProof] = field(default_factory=dict)
    seen_update_ids: set[int] = field(default_factory=set)


def _connectors() -> Any:
    """Return the connectors public module, imported lazily because connectors depends on ingestion."""
    from modules.connectors import public as connectors

    return connectors


def _fail(status_code: int, detail: str) -> DomainError:
    """Build the client-visible rejection raised by every proof check (see ``domain_error_for_status``)."""
    return domain_error_for_status(status_code, detail)


class NativeProofVerifier(Protocol):
    """Strategy verifying one provider's proofs inside the acceptance transaction.

    Implementations must raise a ``DomainError`` subclass with the documented status/detail for every rejected proof,
    must not commit or roll back, and must respect the lock order listed in the module docstring (a hook may
    only take the locks of its own phase).
    """

    providers: ClassVar[tuple[str, ...]]
    revision_policy: ClassVar[RevisionPolicy]

    def check_capture(self, ctx: NativeAcceptance) -> None:
        """Validate the shape of the captured credential operations before any fence is taken."""
        ...

    async def verify_admission(self, ctx: NativeAcceptance) -> None:
        """Verify the provider credential/proof under the Source lock, before collector credential rows."""
        ...

    async def verify_grant(self, ctx: NativeAcceptance) -> None:
        """Verify the provider OAuth/binding grant after the collector authorization, before the state row."""
        ...

    async def verify_transition(self, ctx: NativeAcceptance, state: SourceIngestionState) -> None:
        """Recompute the cursor transition from the locked reservation and set ``ctx.cursor_after``."""
        ...

    def verify_records(self, ctx: NativeAcceptance) -> list[ProviderRecord]:
        """Validate every supplied record against the provider proof and return the persistable rows."""
        ...

    async def settle_provider_state(
        self, ctx: NativeAcceptance, *, batch_id: UUID, has_records: bool, changes: list[ReplayDraft],
    ) -> bool:
        """Apply provider side effects after the state mutation; return ``True`` when the Source was paused."""
        ...


class BaseNativeVerifier:
    """Default verifier: no provider-specific proofs, generic metadata checks, foreign-proof rejection.

    Subclasses override only the hooks they own. The ``owns_*`` flags decide which cross-provider proof
    fields are legal, so a proof for another provider is always a 422 and never ignored.
    """

    providers: ClassVar[tuple[str, ...]] = ()
    revision_policy: ClassVar[RevisionPolicy] = "forbidden"
    owns_telegram_proof: ClassVar[bool] = False
    owns_github_proof: ClassVar[bool] = False
    owns_google_proof: ClassVar[bool] = False
    owns_reddit_proof: ClassVar[bool] = False

    def check_capture(self, ctx: NativeAcceptance) -> None:
        """Reject any credential-operation capture: this provider carries none."""
        if ctx.params.native_operation_id is not None or ctx.params.world_credential_operation_id is not None:
            raise _fail(422, "Credential operation capture is not valid for this provider")

    async def verify_admission(self, ctx: NativeAcceptance) -> None:
        """Run the provider credential check, rejecting foreign Telegram/GitHub proof fields in the fixed order."""
        if not self.owns_telegram_proof:
            self._reject_foreign_telegram(ctx)
        await self._admit_provider(ctx)
        if not self.owns_github_proof:
            self._reject_foreign_github(ctx)

    async def _admit_provider(self, ctx: NativeAcceptance) -> None:
        """Provider-specific credential proof; the generic provider has none."""

    @staticmethod
    def _reject_foreign_telegram(ctx: NativeAcceptance) -> None:
        """Telegram deliveries are only valid for the Telegram provider."""
        if ctx.payload.telegram_raw_deliveries or ctx.payload.telegram_deliveries:
            raise _fail(422, "Telegram proof is not valid for this provider")

    @staticmethod
    def _reject_foreign_github(ctx: NativeAcceptance) -> None:
        """A GitHub segment is only valid for the GitHub provider."""
        if ctx.payload.github_segment is not None:
            raise _fail(422, "GitHub proof is not valid for this provider")

    async def verify_grant(self, ctx: NativeAcceptance) -> None:
        """Check the Google then Reddit proof shape (and own grant row) in the original order."""
        await self._verify_oauth_proof(ctx, "google", owned=self.owns_google_proof)
        await self._verify_oauth_proof(ctx, "reddit", owned=self.owns_reddit_proof)

    @staticmethod
    async def _verify_oauth_proof(ctx: NativeAcceptance, kind: Literal["google", "reddit"], *, owned: bool) -> None:
        """Require the ``kind`` proof exactly when this provider owns it and re-read its grant under lock.

        The grant row is locked ``FOR UPDATE`` (after the collector authorization) and must equal the
        captured operation/token revision, the provisioning credential revision, the source generation and
        the connector revision, and belong to the admitted actor and workspace.
        """
        payload, source = ctx.payload, ctx.source
        operation_id = getattr(payload, f"{kind}_operation_id")
        token_revision = getattr(payload, f"{kind}_token_revision")
        present = operation_id is not None and token_revision is not None
        label = kind.capitalize()
        if owned != present or (not owned and (operation_id is not None or token_revision is not None)):
            raise _fail(422, f"{label} credential proof does not match the provider")
        if not owned:
            return
        from modules.connectors.public import (
            ConnectorOAuthGrantReadProjection as ConnectorOAuthGrant,
        )
        from modules.connectors.public import (
            ConnectorProvisioningReadProjection as ConnectorProvisioning,
        )

        session = ctx.session
        grant = await session.scalar(select(ConnectorOAuthGrant).where(
            ConnectorOAuthGrant.source_id == source.id,
        ).with_for_update())
        credential_revision = await session.scalar(select(ConnectorProvisioning.credential_revision).where(
            ConnectorProvisioning.source_id == source.id,
        ))
        if (grant is None or grant.state != "ready" or grant.encrypted_tokens is None
                or grant.workspace_id != source.workspace_id or grant.owner_id != _actor_id(ctx.scope)
                or grant.provider != source.provider or grant.source_generation != payload.source_generation
                or grant.configuration_revision != payload.connector_revision
                or grant.credential_revision != credential_revision
                or grant.operation_id != operation_id
                or grant.token_revision != token_revision):
            raise _fail(409, f"{label} grant changed during collection")

    async def verify_transition(self, ctx: NativeAcceptance, state: SourceIngestionState) -> None:
        """Default transition: the supplied cursor is authoritative."""
        ctx.cursor_after = ctx.payload.cursor_after

    def verify_records(self, ctx: NativeAcceptance) -> list[ProviderRecord]:
        """Validate record metadata for every record and collect the persistable rows."""
        rows: list[ProviderRecord] = []
        for record in ctx.payload.records:
            metadata = self._parse_metadata(ctx, record)
            row = self._verify_record(ctx, record, metadata)
            if row is not None:
                rows.append(row)
        return rows

    @staticmethod
    def _parse_metadata(ctx: NativeAcceptance, record: IngestionRecord) -> Any:
        """Parse and cross-check the typed provider metadata (identity, version, provider, coverage)."""
        from modules.knowledge.documents.public import ProviderRecordMetadata
        try:
            metadata = ProviderRecordMetadata.model_validate(record.metadata.get("provider_record"))
        except (TypeError, ValueError) as exc:
            raise _fail(422, "Native provider metadata is invalid") from exc
        if (metadata.provider != ctx.source.provider or metadata.identity != record.provider_id
                or metadata.provider_version != record.version):
            raise _fail(422, "Native provider identity is invalid")
        if metadata.coverage != ctx.payload.coverage:
            raise _fail(422, "Native provider coverage is inconsistent")
        return metadata

    def _verify_record(self, ctx: NativeAcceptance, record: IngestionRecord, metadata: Any) -> ProviderRecord | None:
        """Provider hook for one record; ``None`` skips it (acknowledged replay). Generic: persist as supplied."""
        return record, record.model_dump(mode="json", exclude={"collected_at"}), None

    async def settle_provider_state(
        self, ctx: NativeAcceptance, *, batch_id: UUID, has_records: bool, changes: list[ReplayDraft],
    ) -> bool:
        """No provider side effects; the Source is never paused by a generic provider."""
        return False


class TelegramVerifier(BaseNativeVerifier):
    """Telegram: recompute delivery proofs from raw Bot API updates and the verified bot binding."""

    providers: ClassVar[tuple[str, ...]] = ("telegram",)
    owns_telegram_proof: ClassVar[bool] = True

    def check_capture(self, ctx: NativeAcceptance) -> None:
        """Require the original native credential operation and forbid the world-credential capture."""
        if (not isinstance(ctx.params.native_operation_id, UUID)
                or ctx.params.world_credential_operation_id is not None):
            raise _fail(422, "Original Telegram credential operation is required")

    async def _admit_provider(self, ctx: NativeAcceptance) -> None:
        """Compare the live native credential snapshot to the captured operation and access fence."""
        payload, source, lease = ctx.payload, ctx.source, ctx.lease
        credential = await _connectors().get_native_credential_snapshot(
            ctx.session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled)
        if (
            credential is None or credential.source_generation != payload.source_generation
            or credential.configuration_revision != payload.connector_revision
            or credential.operation_id != ctx.params.native_operation_id
            or credential.access_fence != ctx.expected_access_fence
            or credential.workspace_id != lease.workspace_id or credential.source_id != lease.source_id
            or credential.state != "ready" or not credential.verified_bot_id
            or credential.encrypted_token is None or credential.validated_at is None
        ):
            raise _fail(409, "Native Telegram credential is not ready")
        ctx.bot_id = credential.verified_bot_id
        if any(item.update_id != item.update.get("update_id") for item in payload.telegram_raw_deliveries):
            raise _fail(422, "Telegram update identity is invalid")

    async def verify_transition(self, ctx: NativeAcceptance, state: SourceIngestionState) -> None:
        """Reclassify the raw page against the stored cursor and require the supplied proofs/cursor to match."""
        payload = ctx.payload
        current_cursor = _decode_telegram_cursor(state.cursor)
        classification: TelegramProbeClassification = classify_telegram_probe(
            current_cursor, payload.telegram_raw_deliveries,
            received_at=payload.collected_at, verified_bot_id=ctx.bot_id or "",
        )
        if classification.conflict_code:
            raise _fail(409, classification.conflict_code)
        if classification.delivery_proofs != tuple(payload.telegram_deliveries):
            raise _fail(409, "Telegram delivery proof is stale or invalid")
        computed_cursor = (
            _encode_telegram_cursor(classification.cursor_after) if classification.cursor_after else state.cursor
        )
        if computed_cursor != payload.cursor_after:
            raise _fail(409, "Telegram cursor transition is invalid")
        ctx.replay_ids = set(classification.replay_update_ids)
        ctx.accepted_proofs = {
            proof.update_id: proof for proof in classification.delivery_proofs
            if proof.update_id not in ctx.replay_ids
        }
        ctx.cursor_after = payload.cursor_after

    def _verify_record(self, ctx: NativeAcceptance, record: IngestionRecord, metadata: Any) -> ProviderRecord | None:
        """Bind the record to its raw delivery and proof; known exact replays are skipped, not versioned."""
        source, payload = ctx.source, ctx.payload
        detail = metadata.telegram
        proof = ctx.accepted_proofs.get(detail.update_id) if detail is not None else None
        raw = next((item for item in payload.telegram_raw_deliveries
                    if detail is not None and item.update_id == detail.update_id), None)
        allowed_chats = tuple(typing_cast("Iterable[str]", source.configuration.get("telegram_chat_ids", ())))
        if (
            detail is None or proof is None or raw is None or detail.bot_id != ctx.bot_id
            or detail.channel_id not in allowed_chats
            or proof.raw_update_sha256 != raw.raw_update_sha256
            or detail.raw_update_sha256 is not None
            or detail.update_id in ctx.seen_update_ids
        ):
            # A known exact replay is acknowledged in the cursor but creates no new version.
            if detail is not None and detail.update_id in ctx.replay_ids:
                return None
            raise _fail(422, "Telegram document proof is invalid")
        try:
            validate_telegram_record_delivery(record, metadata, proof, raw, allowed_chat_ids=allowed_chats)
        except (TypeError, ValueError) as exc:
            raise _fail(422, "Telegram document does not match its raw delivery") from exc
        ctx.seen_update_ids.add(detail.update_id)
        record_data = record.model_dump(mode="json", exclude={"collected_at"})
        envelope: dict[str, object] = {"proof": proof.model_dump(mode="json"), "raw_update": raw.model_dump(mode="json")}
        persisted_size = len(json.dumps(
            {**record_data, "_native_telegram": envelope}, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if persisted_size > TELEGRAM_OBSERVATION_MAX_BYTES:
            raise _fail(413, "Telegram observation exceeds normalization size limit")
        return record, record_data, envelope


class AlphaVantageVerifier(BaseNativeVerifier):
    """Alpha Vantage: the original world credential operation must still be current."""

    providers: ClassVar[tuple[str, ...]] = ("alpha_vantage",)

    def check_capture(self, ctx: NativeAcceptance) -> None:
        """Require the original world credential operation and forbid the native-credential capture."""
        if (not isinstance(ctx.params.world_credential_operation_id, UUID)
                or ctx.params.native_operation_id is not None):
            raise _fail(422, "Original Alpha credential operation is required")

    async def _admit_provider(self, ctx: NativeAcceptance) -> None:
        """Validate the captured world credential operation under the held access/Source fences."""
        payload, source = ctx.payload, ctx.source
        if not await _connectors().validate_world_credential_operation_in_uow(
            ctx.session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision,
            expected_operation_id=ctx.params.world_credential_operation_id,
            scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=ctx.access_fence, source_fence=ctx.source_fence,
        ):
            raise _fail(409, "Original Alpha credential operation changed")


def needs_visibility_fence(proof: GitHubSegmentProof) -> bool:
    """Return whether the typed hint outcome could pause the Source (so visibility rows are prepared early).

    The proof chooses lock intent only; the live reservation and segment validation must still succeed before
    any acknowledgement or lifecycle mutation happens.
    """
    claim = proof.hint_claim
    if claim is None:
        return False
    return bool(
        proof.target_outcome == "forbidden"
        or claim.intent == "reconcile"
        and claim.locator_kind == "repository"
        and proof.target_outcome == "not_found"
        or claim.intent in {"refresh", "delete_candidate"}
        and claim.locator_kind in {"number", "release_id", "sha"}
        and proof.target_outcome in {"not_found", "forbidden", "partial"}
        or claim.intent == "visibility_check"
        and proof.target_outcome == "not_found"
        and claim.locator_kind in {"repository", "installation"}
    )


class GitHubVerifier(BaseNativeVerifier):
    """GitHub: binding-fence grant proof, segment recomputation and targeted-hint acknowledgement."""

    providers: ClassVar[tuple[str, ...]] = ("github",)
    owns_github_proof: ClassVar[bool] = True

    async def _admit_provider(self, ctx: NativeAcceptance) -> None:
        """Parse the segment proof and, if it may pause the Source, prepare the visibility rows before Tools.

        Visibility preparation takes the credential rows itself, so the orchestrator then skips its own
        ``lock_source_credentials_in_uow`` (never re-enter that set).
        """
        from modules.connectors.public import GitHubSegmentProof

        payload = ctx.payload
        if payload.github_segment is None or payload.telegram_raw_deliveries or payload.telegram_deliveries:
            raise _fail(422, "GitHub collection proof is required")
        try:
            proof = GitHubSegmentProof.model_validate(payload.github_segment.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise _fail(422, "GitHub segment proof is malformed") from exc
        if proof.fence.connector_revision != payload.connector_revision:
            raise _fail(409, "GitHub collection revision is stale")
        ctx.github_proof = proof
        ctx.needs_visibility_fence = needs_visibility_fence(proof)
        if ctx.needs_visibility_fence:
            await sources.prepare_source_pause_for_connector_in_uow(
                ctx.session, ctx.source.id, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
                access_fence=ctx.access_fence, source_fence=ctx.source_fence,
            )

    async def verify_grant(self, ctx: NativeAcceptance) -> None:
        """Lock the GitHub binding fence and require it to equal the proof's fence, then the base checks."""
        payload, source = ctx.payload, ctx.source
        assert ctx.github_proof is not None  # noqa: S101  # mypy narrowing invariant
        ctx.github_fence = await _connectors().lock_github_binding_fence_in_uow(
            ctx.session, source.id, source_generation=payload.source_generation,
            connector_revision=payload.connector_revision, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=ctx.access_fence, source_fence=ctx.source_fence)
        if ctx.github_fence is None or ctx.github_proof.fence != ctx.github_fence:
            raise _fail(409, "GitHub grant is unavailable or requires reconnection")
        await super().verify_grant(ctx)

    async def verify_transition(self, ctx: NativeAcceptance, state: SourceIngestionState) -> None:
        """Revalidate the raw segment against the reserved cursor; supplied records/cursor/coverage must match."""
        payload, source = ctx.payload, ctx.source
        assert ctx.github_proof is not None  # noqa: S101  # mypy narrowing invariant
        segment = await _connectors().validate_github_collection_segment_in_uow(
            ctx.session, source=source, reserved_cursor_before=state.cursor,
            proof=ctx.github_proof, binding_fence=typing_cast("GitHubBindingFence", ctx.github_fence),
            access_fence=ctx.access_fence, source_fence=ctx.source_fence, scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled)
        supplied = [record.model_dump(mode="json", exclude={"collected_at"}) for record in payload.records]
        validated = [record.model_dump(mode="json", exclude={"collected_at"}) for record in segment.records]
        if (supplied != validated or payload.cursor_after != segment.cursor_after
                or payload.coverage != segment.coverage):
            raise _fail(409, "GitHub collection transition is invalid")
        if ctx.needs_visibility_fence and (segment.records or segment.cursor_after != state.cursor):
            # A paused G receipt cannot queue records retired by this same transaction.
            raise _fail(409, "GitHub visibility pause requires an empty unchanged segment")
        ctx.cursor_after = segment.cursor_after

    async def settle_provider_state(
        self, ctx: NativeAcceptance, *, batch_id: UUID, has_records: bool, changes: list[ReplayDraft],
    ) -> bool:
        """Acknowledge the targeted hint and, when visibility is unverified, pause the Source in this UoW.

        Possible pause: lock the claim plus every pause-eligible hint before capacity, then acknowledge with
        the already-prepared rows (never reacquiring). A current target 404 may mean deletion or lost private
        access; pausing fences current evidence while retained owner history stays reviewable. ``changes[0]``
        is replaced by the paused Source change.
        """
        proof = ctx.github_proof
        if proof is None or proof.hint_claim is None:
            return False
        from modules.connectors.public import GitHubHintClaim

        connectors = _connectors()
        source = ctx.source
        try:
            claim = GitHubHintClaim.model_validate(proof.hint_claim.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise _fail(409, "GitHub hint claim is stale") from exc
        deletion_unverified = proof.hint_claim.intent == "delete_candidate" and proof.target_outcome != "found"
        # A signed delete hint plus a missing/forbidden read lacks the exact current version proof for a tombstone.
        visibility_unverified = proof.target_outcome in {"not_found", "forbidden", "partial"}
        disposition: Literal["accepted_ingestion", "completed", "visibility_unverified"] = (
            "visibility_unverified" if deletion_unverified or visibility_unverified
            else "accepted_ingestion" if has_records else "completed"
        )
        reconcile_next_page = proof.next_page if claim.intent == "reconcile" and proof.has_next else None
        if ctx.needs_visibility_fence:
            await connectors.lock_github_hints_for_visibility_pause_in_uow(
                ctx.session, claim=claim, binding_fence=typing_cast("GitHubBindingFence", ctx.github_fence),
                scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
                access_fence=ctx.access_fence, source_fence=ctx.source_fence,
            )
            acknowledged = await connectors.acknowledge_github_hint_in_uow(
                ctx.session, claim=claim, batch_id=batch_id, disposition=disposition,
                reconcile_next_page=reconcile_next_page,
                scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
                access_fence=ctx.access_fence, source_fence=ctx.source_fence,
            )
        else:
            acknowledged = await connectors.acknowledge_github_hint(
                ctx.session, claim=claim, batch_id=batch_id, disposition=disposition,
                reconcile_next_page=reconcile_next_page,
                scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
            )
        if not acknowledged:
            raise _fail(409, "GitHub hint claim changed during acceptance")
        if not ctx.needs_visibility_fence:
            return False
        paused = await sources.pause_source_for_connector_in_uow(
            ctx.session, source.id, scope=ctx.scope, multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=ctx.access_fence, source_fence=ctx.source_fence)
        if paused is None:
            raise _fail(409, "GitHub source changed during visibility confirmation")
        changes[0] = make_source_change(paused.id, paused.generation, paused.status, scope=ctx.scope)
        return True


class GoogleOAuthVerifier(BaseNativeVerifier):
    """Google Mail/Calendar/Drive: the OAuth grant row must equal the captured operation and token revision."""

    providers: ClassVar[tuple[str, ...]] = ("google_mail", "google_calendar", "google_drive")
    owns_google_proof: ClassVar[bool] = True


class RedditVerifier(BaseNativeVerifier):
    """Reddit: the OAuth grant row must equal the captured operation and token revision."""

    providers: ClassVar[tuple[str, ...]] = ("reddit",)
    owns_reddit_proof: ClassVar[bool] = True


class RevisionedRestVerifier(BaseNativeVerifier):
    """Notion, Slack and X: an immutable provider credential revision proof is mandatory."""

    providers: ClassVar[tuple[str, ...]] = ("notion", "slack", "x")
    revision_policy: ClassVar[RevisionPolicy] = "required"


class OptionalRevisionRestVerifier(BaseNativeVerifier):
    """Mastodon: the credential revision proof may be omitted, but is validated when supplied."""

    providers: ClassVar[tuple[str, ...]] = ("mastodon",)
    revision_policy: ClassVar[RevisionPolicy] = "optional"


_REGISTRY: dict[str, NativeProofVerifier] = {}
_DEFAULT_VERIFIER: NativeProofVerifier = BaseNativeVerifier()


def register_native_verifier(verifier: NativeProofVerifier) -> None:
    """Register ``verifier`` for each provider id it declares (last registration wins).

    Adding a native provider is a registration, not an edit of the acceptance orchestrator.
    """
    for provider in verifier.providers:
        _REGISTRY[provider] = verifier


def native_verifier_for(provider: str | None) -> NativeProofVerifier:
    """Return the verifier registered for ``provider``, or the generic no-proof verifier."""
    return _REGISTRY.get(provider or "", _DEFAULT_VERIFIER)


for _verifier in (
    TelegramVerifier(), AlphaVantageVerifier(), GitHubVerifier(), GoogleOAuthVerifier(), RedditVerifier(),
    RevisionedRestVerifier(), OptionalRevisionRestVerifier(),
):
    register_native_verifier(_verifier)
