"""Built-in ``ProviderCollector`` strategies for ``native_fetch.run_provider_fetch``.

Each class collects one bounded page for a family of native providers. Importing this module
registers them in the ``native_fetch`` registry (``resolve_collector`` imports it lazily), so a
new provider adds a class plus one ``register_collector`` call here or in its own module and
never edits the dispatcher. Provider modules are imported inside ``collect`` to keep API start-up
light and to avoid import cycles.
"""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select

from core.errors import ConflictError, UnavailableError, ValidationFailedError
from modules.connectors import provisioning
from modules.connectors import public as connectors_public
from modules.connectors.github import oauth as github_oauth
from modules.connectors.github.adapter import collect_github_segment
from modules.connectors.github.schemas import (
    GitHubBindingFence,
    GitHubHintClaimProof,
    project_github_source_config,
)
from modules.connectors.github.sync import validate_github_segment
from modules.connectors.models import GithubOAuthGrant
from modules.connectors.native_fetch import (
    GITHUB_RECONNECT_DETAIL,
    AcceptedCollection,
    BaseProviderCollector,
    CollectedPage,
    FetchContext,
    ProviderAdmissionBusy,
    acquire_github_network_permit,
    await_with_github_segment_deadline,
    extend_provider_cooldown,
    provider_cooldown,
    rate_limited_read,
    register_collector,
    register_default_collector,
)
from modules.connectors.public import (
    ProviderRateLimited,
    get_native_credential_snapshot,
)
from modules.ingestion import public as ingestion
from modules.ingestion.public import (
    ConnectorCollectionLease,
    NativeCollectionBatch,
    NativeCollectionReceipt,
    classify_telegram_probe,
)


def _page_result(page: Any, collected_at: datetime) -> CollectedPage:
    """Wrap a provider page (``records``/``coverage``/``next_eligible_at``) for generic acceptance."""
    return CollectedPage(
        records=list(page.records),
        coverage=page.coverage,
        collected_at=collected_at,
        next_eligible_at=page.next_eligible_at,
    )


class FeedCollector(BaseProviderCollector):
    """YouTube and arXiv feeds: one fenced HTTP read per send, no extra credential proof."""

    async def collect(self, ctx: FetchContext) -> CollectedPage:
        """Collect one feed page; every physical request re-runs the native send fence."""
        from modules.connectors.providers.feed_catalog import collect_provider_feed

        collected_at = datetime.now(UTC)
        page = await collect_provider_feed(
            ctx.source,
            collected_at=collected_at,
            session_factory=ctx.request.app.state.session_factory,
            before_request=ctx.send_fence,
        )
        return _page_result(page, collected_at)


class HuggingFaceCollector(BaseProviderCollector):
    """Hugging Face model listings: keyless, fenced per request."""

    async def collect(self, ctx: FetchContext) -> CollectedPage:
        """Collect one model-listing page behind the native send fence."""
        from modules.connectors.providers.research import collect_huggingface_models

        collected_at = datetime.now(UTC)
        page = await collect_huggingface_models(
            ctx.source,
            collected_at=collected_at,
            before_request=ctx.send_fence,
        )
        return _page_result(page, collected_at)


class GithubReleasesCollector(BaseProviderCollector):
    """Default collector: public GitHub releases for providers without a dedicated strategy."""

    async def collect(self, ctx: FetchContext) -> CollectedPage:
        """Collect one releases page behind the native send fence."""
        from modules.connectors.providers.social import collect_github_releases

        collected_at = datetime.now(UTC)
        page = await collect_github_releases(
            ctx.source,
            collected_at=collected_at,
            before_request=ctx.send_fence,
        )
        return _page_result(page, collected_at)


class _WorldCapture:
    """Mutable record of the Alpha operation id captured before the first send."""

    def __init__(self) -> None:
        """Start with no captured operation."""
        self.operation_id: UUID | None = None
        self.seen = False


class WorldDataCollector(BaseProviderCollector):
    """Alpha Vantage and Open-Meteo: Alpha's credential operation is captured once and compared."""

    async def collect(self, ctx: FetchContext) -> CollectedPage:
        """Collect one world-data page; Alpha pages carry a post-collect credential proof."""
        from modules.connectors.providers.world_data import collect_world_data

        collected_at = datetime.now(UTC)
        capture = _WorldCapture()
        is_alpha = ctx.source.provider == "alpha_vantage"

        async def before_world_request(credential_operation_id: UUID | None) -> None:
            """Capture the pre-I/O Alpha operation once and compare it on every GET."""
            if is_alpha:
                if not isinstance(credential_operation_id, UUID):
                    raise ConflictError("Original Alpha credential capture is missing")
                if capture.seen and credential_operation_id != capture.operation_id:
                    raise ConflictError("Original Alpha credential operation changed")
                capture.operation_id = credential_operation_id
                capture.seen = True
            elif credential_operation_id is not None:
                raise ConflictError("Unexpected world credential capture")
            await ctx.send_fence(world_operation_id=capture.operation_id)

        page = await collect_world_data(
            ctx.source,
            collected_at=collected_at,
            settings=ctx.request.app.state.settings,
            session=ctx.session,
            redis=ctx.request.app.state.redis,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=ctx.access_fence,
            source_fence=ctx.source_fence,
            before_request=before_world_request,
        )
        result = _page_result(page, collected_at)
        if is_alpha:
            result.world_operation_id = capture.operation_id

            async def verify_capture() -> None:
                """Require the page's credential operation to equal the captured one, then fence again."""
                if (
                    not capture.seen
                    or capture.operation_id is None
                    or page.credential_operation_id != capture.operation_id
                ):
                    raise ConflictError("Alpha credential capture changed during collection")
                await ctx.send_fence(world_operation_id=capture.operation_id)

            result.post_collect = verify_capture
        return result


class GithubCollector(BaseProviderCollector):
    """GitHub segment collection under a source/revision-bound grant and a cluster-shared permit."""

    async def prepare(self, ctx: FetchContext) -> None:
        """Claim the due durable hint (if any) before the collection window opens.

        Runs inside the GitHub segment deadline; the dispatcher releases the lease on any failure.
        """
        ctx.prepared = await await_with_github_segment_deadline(
            connectors_public.claim_github_hint(
                ctx.session,
                source_id=ctx.source.id,
                source_generation=ctx.payload.source_generation,
                connector_revision=ctx.payload.connector_revision,
                multi_workspace_enabled=ctx.multi_workspace_enabled,
                scope=ctx.scope,
            ),
            deadline=ctx.segment_deadline,
        )

    async def _open_grant(
        self, ctx: FetchContext
    ) -> tuple[GitHubBindingFence, tuple[UUID, int], str, Any]:
        """Lock the connector and grant, decrypt the access token, and release SQL.

        Lock order: connector, GitHub binding fence, then the grant row (``FOR UPDATE``). Returns
        ``(fence, (operation_id, token_revision), access_token, projected_config)`` after a
        rollback so no lock is held across provider I/O. Raises 409 (reconnect) for a missing,
        expired or mismatched grant and ``ValueError`` when the token cannot be opened.
        """
        source, payload = ctx.source, ctx.payload
        await provisioning.lock_connector(
            ctx.session,
            source.id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            expected_access_fence=ctx.access_fence,
        )
        fence = await connectors_public.lock_github_binding_fence_in_uow(
            ctx.session,
            source.id,
            source_generation=payload.source_generation,
            connector_revision=payload.connector_revision,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            access_fence=ctx.access_fence,
            source_fence=ctx.source_fence,
        )
        if fence is None:
            raise ConflictError(GITHUB_RECONNECT_DETAIL)
        grant = await ctx.session.scalar(
            select(GithubOAuthGrant)
            .where(GithubOAuthGrant.source_id == source.id)
            .with_for_update()
        )
        if (
            grant is None
            or grant.state != "ready"
            or grant.encrypted_tokens is None
            or grant.source_generation != source.generation
            or grant.configuration_revision != payload.connector_revision
            or grant.expires_at is None
            or grant.expires_at <= datetime.now(UTC)
        ):
            raise ConflictError(GITHUB_RECONNECT_DETAIL)
        binding = (grant.operation_id, grant.token_revision)
        key = ctx.request.app.state.settings.connector_credential_encryption_key.get_secret_value()
        token_pair = github_oauth._open_token_cipher(
            key,
            grant.encrypted_tokens,
            source.id,
            grant.operation_id,
            grant.source_generation,
            grant.configuration_revision,
        )
        access_token = token_pair.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("github_grant_unavailable")
        config = project_github_source_config(source.configuration)
        await ctx.session.rollback()
        return fence, binding, access_token, config

    async def collect(self, ctx: FetchContext) -> CollectedPage:
        """Collect and validate one GitHub segment; the batch carries the raw proof.

        Each send re-checks the cooldown, the original lease and the exact grant binding, then
        acquires the cluster-shared network permit (busy raises ``ProviderAdmissionBusy``). The
        permit token is stored on ``ctx`` so the dispatcher releases it in ``finally``. Before
        acceptance the grant binding is re-proved once more.
        """
        collected_at = datetime.now(UTC)
        fence, binding, access_token, config = await self._open_grant(ctx)
        hint_claim = ctx.prepared

        async def before_github_request() -> None:
            """Check original lease and exact grant proof, release SQL, then reserve network capacity."""
            cooldown_at = await provider_cooldown(ctx.request, "github")
            if cooldown_at is not None and cooldown_at > datetime.now(UTC):
                raise ProviderRateLimited(cooldown_at)
            await ctx.send_fence(github_fence=fence, github_binding=binding)
            ctx.github_permit_token = await acquire_github_network_permit(ctx.request)
            if ctx.github_permit_token is None:
                raise ProviderAdmissionBusy("GitHub provider slot is busy")

        proof = await collect_github_segment(
            config,
            access_token,
            fence=fence,
            cursor_before=ctx.lease.cursor_before,
            collected_at=collected_at,
            before_request=before_github_request,
            hint_claim=(
                GitHubHintClaimProof.model_validate(hint_claim.model_dump(mode="python"))
                if hint_claim is not None
                else None
            ),
        )
        validated = validate_github_segment(
            fence,
            config,
            ctx.lease.cursor_before,
            proof,
            collected_at=collected_at,
        )

        async def reprove_binding() -> None:
            """Re-prove the original grant binding immediately before Ingestion accepts."""
            await ctx.send_fence(github_fence=fence, github_binding=binding)

        return CollectedPage(
            records=list(validated.records),
            coverage=validated.coverage,
            collected_at=collected_at,
            github_segment=proof,
            cursor_after=validated.cursor_after,
            has_cursor_after=True,
            pre_accept=reprove_binding,
        )


class TelegramCollector(BaseProviderCollector):
    """Telegram bot updates: probe without offset, continue only after durable page receipts."""

    async def collect(self, ctx: FetchContext) -> AcceptedCollection:
        """Run one Telegram trigger (at most five probe pages) and return the accepted outcome."""
        return await _TelegramRun(ctx).run()


class _TelegramRun:
    """One Telegram trigger: credential load, bounded probe loop, and page acceptance.

    ``ctx.active_lease[0]`` is updated whenever continuation reserves a new page so the
    dispatcher releases the matching reservation on provider errors or cancellation. A
    provider retry deadline is shared across Telegram sources and ends the run without
    accepting a page or advancing its cursor. Limits: 25 MiB transport and 500 updates.
    """

    def __init__(self, ctx: FetchContext) -> None:
        """Bind the request context; the credential and cursor are loaded in ``run``."""
        self.ctx = ctx
        self.lease: ConnectorCollectionLease = ctx.lease
        self.snapshot: Any = None
        self.token = ""
        self.cursor: Any = None
        self.offset: int | None = None
        self.total_updates = 0
        self.total_bytes = 0

    async def _load_credential(self) -> None:
        """Lock the connector, read and decrypt the verified bot token, then release SQL.

        Raises 409 when the native credential is not ready and 503 when it cannot be
        decrypted; both roll back first so no lock is held across Telegram I/O.
        """
        from modules.connectors.credentials import decrypt_native_token

        ctx = self.ctx
        await provisioning.lock_connector(
            ctx.session,
            ctx.source.id,
            provisioning._ALL_CREDENTIAL_SLOTS,
            scope=ctx.scope,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            expected_access_fence=ctx.access_fence,
        )
        snapshot = await get_native_credential_snapshot(
            ctx.session,
            ctx.source.id,
            source_generation=self.lease.source_generation,
            connector_revision=self.lease.connector_revision,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            scope=ctx.scope,
        )
        if (
            snapshot is None
            or snapshot.state != "ready"
            or not snapshot.encrypted_token
            or not snapshot.verified_bot_id
        ):
            await ctx.session.rollback()
            raise ConflictError("Native Telegram credential is not ready")
        try:
            self.token = decrypt_native_token(
                ctx.request.app.state.settings.connector_credential_encryption_key.get_secret_value(),
                snapshot,
            )
        except Exception as exc:
            await ctx.session.rollback()
            raise UnavailableError("Native Telegram credential is unavailable") from exc
        self.snapshot = snapshot
        await ctx.session.rollback()

    async def _fetch(self, remaining_bytes: int) -> Any:
        """Fence, then fetch one update page; a provider retry deadline yields a rate-limited outcome.

        The deadline extends the shared cooldown and releases the current lease without any receipt.
        """
        from modules.connectors.providers.telegram import fetch_telegram_updates

        ctx = self.ctx
        try:
            await ctx.send_fence(lease=self.lease, native_snapshot=self.snapshot)
            return await fetch_telegram_updates(
                self.token,
                offset=self.offset,
                remaining_bytes=min(10 * 1024 * 1024, remaining_bytes),
                # Invoked within this iteration before ``self.lease`` is rebound.
                before_request=lambda: ctx.send_fence(
                    lease=self.lease, native_snapshot=self.snapshot
                ),
            )
        except ProviderRateLimited as exc:
            deadline = await extend_provider_cooldown(
                ctx.request, ctx.source.provider or "telegram", exc.next_eligible_at
            )
            await ingestion.release_connector_collection(
                ctx.session,
                self.lease,
                error_code="provider_rate_limited",
                multi_workspace_enabled=ctx.multi_workspace_enabled,
                scope=ctx.scope,
            )
            return AcceptedCollection(
                rate_limited=rate_limited_read(ctx.scope, "pending_updates_only", deadline),
                eligible=deadline,
            )

    def _account(self, page: Any, remaining_bytes: int) -> None:
        """Charge the page against the byte and update ceilings (422 when exceeded)."""
        if page.transport_bytes > remaining_bytes:
            raise ValidationFailedError("Telegram trigger byte limit exceeded")
        self.total_bytes += page.transport_bytes
        if self.total_updates + len(page.deliveries) > 500:
            raise ValidationFailedError("Telegram trigger update limit exceeded")
        self.total_updates += len(page.deliveries)

    async def _skip_replay_page(self, page_number: int) -> AcceptedCollection | None:
        """Release the lease of an all-replay page and re-reserve for the next offset.

        Returns an empty ``succeeded`` receipt when the probe cannot continue (last page or no
        usable cursor); returns ``None`` after re-acquiring so the loop continues. The new lease
        is published into ``active_lease`` only after its access revision is verified.
        """
        ctx = self.ctx
        lease = self.lease
        await ingestion.release_connector_collection(
            ctx.session,
            lease,
            error_code=None,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            scope=ctx.scope,
        )
        cursor = self.cursor
        if page_number == 4 or cursor is None or cursor.last_update_id >= 2**63 - 1:
            return AcceptedCollection(
                receipt=NativeCollectionReceipt(
                    workspace_id=lease.workspace_id,
                    actor_user_id=lease.actor_user_id,
                    membership_revision=lease.membership_revision,
                    batch_id=None,
                    run_id=None,
                    status="succeeded",
                    received_update_count=0,
                    record_count=0,
                    coverage="pending_updates_only",
                    cursor_after=lease.cursor_before,
                )
            )
        self.lease = await ingestion.acquire_connector_collection(
            ctx.session,
            source_id=ctx.source.id,
            source_generation=lease.source_generation,
            connector_revision=lease.connector_revision,
            collector_token=ctx.collector_token,
            request_ref=ctx.request_ref,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            scope=ctx.scope,
        )
        if self.lease.configuration_revision != ctx.access_fence.configuration_revision:
            raise ConflictError("Original Telegram access changed")
        ctx.active_lease[0] = self.lease
        self.offset = cursor.last_update_id + 1
        return None

    async def _accept_page(
        self, page: Any, classification: Any, replay_ids: set[int]
    ) -> AcceptedCollection:
        """Map non-replayed deliveries, re-fence, and have Ingestion accept the page.

        The acceptance settles the admission; a further page needs a new request.
        """
        from modules.connectors.providers.telegram import map_telegram_update

        ctx, lease = self.ctx, self.lease
        proof_by_id = {proof.update_id: proof for proof in classification.delivery_proofs}
        allowed = frozenset(cast("list[str]", ctx.source.configuration["telegram_chat_ids"]))
        records = [
            record
            for delivery in page.deliveries
            if delivery.update_id not in replay_ids
            if (
                record := map_telegram_update(
                    delivery.update,
                    allowed_chat_ids=allowed,
                    proof=proof_by_id[delivery.update_id],
                    collected_at=page.collected_at,
                )
            )
            is not None
        ]
        cursor_after = (
            classification.cursor_after.model_dump_json()
            if classification.cursor_after is not None
            else lease.cursor_before
        )
        native_batch = NativeCollectionBatch(
            source_id=ctx.source.id,
            source_generation=lease.source_generation,
            connector_revision=lease.connector_revision,
            lease_token=lease.token,
            cursor_before=lease.cursor_before,
            cursor_after=cursor_after,
            records=records,
            telegram_deliveries=classification.delivery_proofs,
            telegram_raw_deliveries=page.deliveries,
            coverage="pending_updates_only",
            collected_at=page.collected_at,
        )
        await ctx.send_fence(lease=lease, native_snapshot=self.snapshot)
        receipt = await ingestion.accept_native_collection(
            ctx.session,
            native_batch,
            collector_token=ctx.collector_token,
            lease=lease,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            scope=ctx.scope,
            expected_native_operation_id=self.snapshot.operation_id,
            request_ref=ctx.request_ref,
            expected_world_credential_operation_id=None,
        )
        return AcceptedCollection(receipt=receipt)

    async def run(self) -> AcceptedCollection:
        """Probe up to five pages; return on the first accepted, empty or rate-limited outcome.

        Raises 422 on byte/update ceilings, 409 on a stream conflict and 503 when the loop
        ends without a receipt.
        """
        ctx = self.ctx
        await self._load_credential()
        self.cursor = await ingestion.read_telegram_collection_state(
            ctx.session,
            self.lease,
            multi_workspace_enabled=ctx.multi_workspace_enabled,
            scope=ctx.scope,
        )
        for page_number in range(5):
            remaining_bytes = 25 * 1024 * 1024 - self.total_bytes
            if remaining_bytes <= 0:
                raise ValidationFailedError("Telegram trigger byte limit exceeded")
            page = await self._fetch(remaining_bytes)
            if isinstance(page, AcceptedCollection):
                return page
            self._account(page, remaining_bytes)
            classification = classify_telegram_probe(
                self.cursor,
                page.deliveries,
                received_at=page.collected_at,
                verified_bot_id=self.snapshot.verified_bot_id,
            )
            if classification.conflict_code is not None:
                raise ConflictError("telegram_stream_conflict")
            replay_ids = set(classification.replay_update_ids)
            if page.deliveries and len(replay_ids) == len(page.deliveries):
                done = await self._skip_replay_page(page_number)
                if done is not None:
                    return done
                continue
            return await self._accept_page(page, classification, replay_ids)
        raise UnavailableError("Telegram collection did not produce a receipt")


register_collector(("youtube", "arxiv"), FeedCollector())
register_collector(("huggingface",), HuggingFaceCollector())
register_collector(("github",), GithubCollector())
register_collector(("alpha_vantage", "open_meteo"), WorldDataCollector())
register_collector(("telegram",), TelegramCollector())
register_default_collector(GithubReleasesCollector())
