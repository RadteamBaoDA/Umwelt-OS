"""HarnessContext: per-segment execution state, fenced persistence and cancellation/budget bookkeeping for one agent run."""

import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.auth.public import revalidate_account_session
from core.config import Settings
from core.errors import DomainError
from core.model_gateway.policy import may_send
from core.model_gateway.schemas import AIExecutionConfig, ModelMapping, RequestPolicy
from core.tools import ToolExecutionPrincipal, ToolRegistry, ToolRisk
from core.tools.schemas import ToolOutputFence
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.agents._harness_types import (
    MAX_ACTIVE_SECONDS,
    HarnessState,
    RunCancelled,
    RunLimitReached,
)
from modules.agents.access import admit
from modules.agents.internal_writes import INTERNAL_DESTINATION, is_internal_write
from modules.agents.models import AgentProfile, AgentRun
from modules.agents.public import (
    APPROVAL_WORKFLOW_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    PROMPT_VERSION,
    SPECIALIST_CHECKPOINT_SCHEMA_VERSION,
    SPECIALIST_WORKFLOW_VERSION,
    WORKFLOW_VERSION,
)
from modules.settings import public as settings_public
from modules.tools.public import revalidate_native_output_fences


class HarnessContext:
    """Hold process-local service handles and the PostgreSQL lease for one worker segment."""

    def __init__(
        self,
        run_id: UUID,
        scope: InternalJobScope,
        original_fence: AccessFence,
        claim_generation: int,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        settings: Settings,
        redis: Redis,
        registry: ToolRegistry,
        lease_connection: Any,
        lease_key: int,
        segment_started: float,
        allowed_tools: frozenset[str],
        tool_contracts: dict[str, dict[str, str]],
        active_seconds: int = 0,
        workflow_version: str = WORKFLOW_VERSION,
        prompt_version: str = PROMPT_VERSION,
        profile_snapshot: dict[str, Any] | None = None,
        modules: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind the claimed owner and current worker services outside checkpoint state.

        Workspace, owner and the original access fence are rebuilt from the durable run row
        (Recipe J) by the worker; every fresh snapshot re-admits ``scope`` and requires the live
        fence to equal ``original_fence``. Credentials, service handles, and ORM objects never
        enter graph state.
        """
        self.run_id = run_id
        self.scope = scope
        self.original_fence = original_fence
        self.owner_id = scope.actor_user_id
        self.multi_workspace_enabled: bool = settings.multi_workspace_enabled
        self.claim_generation = claim_generation
        self.session_factory = session_factory
        self.engine = engine
        self.settings = settings
        self.redis = redis
        self.registry = registry
        self.lease_connection = lease_connection
        self.lease_key = lease_key
        self.segment_started = segment_started
        self.allowed_tools = allowed_tools
        self.tool_contracts = tool_contracts
        self.active_seconds = active_seconds
        self.workflow_version = workflow_version
        self.prompt_version = prompt_version
        self.profile_snapshot = profile_snapshot
        # Workspace-effective module map; never written to the shared ToolRegistry (O1).
        self.modules = modules
        self.model_send_attempts = 0
        self.unobservable_model_usage = False
        # 0 for a run's own profile, 1 inside a supervisor handoff; depth never exceeds 1.
        self.handoff_depth = 0

    def elapsed(self) -> float:
        """Return monotonic active time in this live segment, excluding queue waits."""
        return time.monotonic() - self.segment_started

    def remaining_active(self) -> float:
        """Return the run's cumulative active-time remainder across all prior segments."""
        return max(0.0, MAX_ACTIVE_SECONDS - self.active_seconds - self.elapsed())

    def assert_profile_checkpoint(self, state: HarnessState) -> None:
        """Bind schema-2 graph state to the immutable run-row profile and authorized owner record."""
        if self.profile_snapshot is None:
            return
        if (
            state.get("profile_id") != self.profile_snapshot.get("id")
            or state.get("profile_revision_hash")
            != self.profile_snapshot.get("profile_revision_hash")
            or state.get("owner_record_state") != "authorized"
        ):
            raise RunCancelled("Profile checkpoint authority no longer matches its durable run")

    async def admit_original(self, session: AsyncSession, *, lock: bool = False) -> AccessFence:
        """Admit the run's captured scope and require the live fence to equal the original epoch.

        Any revoked owner, membership or configuration change cancels the run (never rebases it).
        ``lock=True`` takes the fence ahead of Source, Document and run locks for a publication.
        """
        try:
            fence = await admit(
                session,
                scope=self.scope,
                multi_workspace_enabled=self.multi_workspace_enabled,
                lock=lock,
                expected=self.original_fence if lock else None,
            )
        except (HTTPException, DomainError) as exc:
            raise RunCancelled("Workspace access is no longer valid") from exc
        if fence != self.original_fence:
            raise RunCancelled("Workspace access changed since the run was created")
        return fence

    async def assert_lease(self) -> None:
        """Require this same PostgreSQL backend session to retain exclusive run ownership.

        Combines re-entrant advisory lock acquisition and immediate release into one SQL
        round-trip; if another session stole or released the lock, ``WHERE`` filters out
        the row and ``scalar`` returns ``None``, raising ``RunCancelled``.
        """
        try:
            # Single round-trip probe: re-entrant try-lock followed immediately by unlock on the same backend.
            held = await self.lease_connection.scalar(
                text("SELECT pg_advisory_unlock(:key) WHERE pg_try_advisory_lock(:key)"),
                {"key": self.lease_key},
            )
            if not held:
                raise RunCancelled("Run lease is no longer held")
            await self.lease_connection.commit()
        except RunCancelled:
            raise
        except Exception as exc:
            raise RunCancelled("Run lease connection is unavailable") from exc

    async def _run_snapshot(self) -> AgentRun:
        """Read the current run and verify workspace epoch, owner, auth session, version, cancellation, and claim fence."""
        async with self.session_factory() as session:
            await self.admit_original(session)
            row = await session.scalar(
                select(AgentRun).where(
                    AgentRun.id == self.run_id,
                    AgentRun.workspace_id == self.scope.workspace_id,
                )
            )
            expected_schema = (
                SPECIALIST_CHECKPOINT_SCHEMA_VERSION
                if self.profile_snapshot is not None
                else CHECKPOINT_SCHEMA_VERSION
            )
            if (
                row is None
                or row.owner_id != self.owner_id
                or row.status != "running"
                or row.cancel_requested
                or row.evidence_revoked
                or row.claim_generation != self.claim_generation
                or row.workflow_version != self.workflow_version
                or row.prompt_version != self.prompt_version
                or row.checkpoint_schema_version != expected_schema
                or (
                    self.profile_snapshot is not None
                    and (
                        row.profile_snapshot != self.profile_snapshot
                        or row.profile_revision_hash
                        != self.profile_snapshot.get("profile_revision_hash")
                        or row.agent_id != self.profile_snapshot.get("id")
                    )
                )
            ):
                raise RunCancelled("Run state or version changed")
            if self.profile_snapshot is not None:
                profile_id = self.profile_snapshot.get("id")
                if not isinstance(profile_id, str):
                    raise RunCancelled("Run profile snapshot is invalid")
                profile = await session.scalar(
                    select(AgentProfile).where(
                        AgentProfile.workspace_id == self.scope.workspace_id,
                        AgentProfile.profile_id == profile_id,
                    )
                )
                if profile is not None:
                    if not profile.enabled:
                        raise RunCancelled("Agent profile is disabled")
                    current_tools = {
                        item.get("name"): item
                        for item in profile.allowed_tools
                        if isinstance(item, dict)
                    }
                    if any(
                        current_tools.get(item.get("name")) != item
                        for item in self.profile_snapshot.get("allowed_tools", [])
                    ):
                        raise RunCancelled("Agent profile tool permission changed")
                    current_sources = {str(item) for item in profile.source_ids}
                    if not set(self.profile_snapshot.get("source_ids", [])) <= current_sources:
                        raise RunCancelled("Agent profile source scope changed")
            detached = AgentRun(
                id=row.id,
                workspace_id=row.workspace_id,
                owner_id=row.owner_id,
                membership_revision=row.membership_revision,
                configuration_revision=row.configuration_revision,
                auth_session_hash=row.auth_session_hash,
                agent_id=row.agent_id,
                workflow_version=row.workflow_version,
                prompt_version=row.prompt_version,
                checkpoint_schema_version=row.checkpoint_schema_version,
                checkpoint_thread_id=row.checkpoint_thread_id,
                prompt=row.prompt,
                allowed_tools=list(row.allowed_tools),
                tool_contracts=dict(row.tool_contracts),
                status=row.status,
                cancel_requested=row.cancel_requested,
                chat_link_required=row.chat_link_required,
                dispatch_generation=row.dispatch_generation,
                claim_generation=row.claim_generation,
                claim_started_at=row.claim_started_at,
                steps=row.steps,
                tool_calls=row.tool_calls,
                active_seconds=row.active_seconds,
                token_usage=row.token_usage,
                token_usage_unknown=row.token_usage_unknown,
                source_fences=dict(row.source_fences),
                token_budget=row.token_budget,
                profile_snapshot=dict(row.profile_snapshot) if row.profile_snapshot else None,
            )
        async with self.session_factory() as session:
            if not await revalidate_account_session(
                session,
                detached.auth_session_hash,
                detached.owner_id,
                multi_workspace_enabled=self.multi_workspace_enabled,
            ):
                raise RunCancelled("Owner session is no longer valid")
        await self.assert_lease()
        return detached

    async def revalidate_principal(self, principal: ToolExecutionPrincipal) -> bool:
        """Recheck run lease, owner session, workflow versions, and exact current registry contracts."""
        try:
            from modules.agents.access import effective_workspace_modules
            from modules.settings.public import read_module_availability

            if principal.scope != self.scope:
                return False
            async with self.session_factory() as availability_session:
                lifecycle = await read_module_availability(
                    availability_session,
                    scope=self.scope,
                    multi_workspace_enabled=self.multi_workspace_enabled,
                )
            self.modules = effective_workspace_modules(lifecycle)
            if not next((item.enabled for item in lifecycle.modules if item.id == "agents"), False):
                return False
            row = await self._run_snapshot()
            if principal.actor_id != f"owner:{row.owner_id}" or not principal.is_owner:
                return False
            available = {
                definition.name: definition
                for definition in self.registry.list_tools(
                    allowed_tools=self.allowed_tools, modules=self.modules
                )
            }
            for name in self.allowed_tools:
                expected = self.tool_contracts.get(name, {})
                current = available.get(name)
                if current is None or not self.supports_definition(current, expected):
                    return False
            return True
        except TypeError:
            raise
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            return False

    def supports_definition(self, definition: Any, expected: dict[str, str]) -> bool:
        """Accept only frozen read contracts or the one versioned, approved webhook action contract."""
        exact = definition.version == expected.get(
            "version"
        ) and definition.schema_fingerprint == expected.get("fingerprint")
        if definition.risk == ToolRisk.READ_ONLY:
            return exact and not definition.confirmation_required
        if (
            exact
            and self.workflow_version == SPECIALIST_WORKFLOW_VERSION
            and is_internal_write(definition)
        ):
            return True
        return bool(
            exact
            and self.workflow_version in {APPROVAL_WORKFLOW_VERSION, SPECIALIST_WORKFLOW_VERSION}
            and definition.name == "webhook.send"
            and definition.risk == ToolRisk.EXTERNAL_WRITE
            and definition.confirmation_required
            and definition.permissions == ("webhook.send",)
        )

    async def authorize_external_send(
        self,
        state: HarnessState,
        profile: Any,
        action_id: UUID,
        definition: Any,
        arguments: dict[str, Any],
        destination_id: str,
    ) -> bool:
        """Revalidate source/session/claim/profile after DNS and reserve the effect before the socket send."""
        from modules.agents.approvals import reserve_effect_before_send
        from modules.tools.public import load_webhook_profiles

        try:
            run = await self._run_snapshot()
            current = load_webhook_profiles(self.settings).get(profile.alias)
            if current is None or current.revision != profile.revision:
                return False
            if state.get("source_fences") and not await revalidate_native_output_fences(
                self.session_factory,
                self.decode_fences(state["source_fences"]),
                self.owner_principal(state, destination_id),
                destination_kind="remote",
                multi_workspace_enabled=self.multi_workspace_enabled,
            ):
                return False
            return await reserve_effect_before_send(
                self.session_factory,
                action_id=action_id,
                run_id=self.run_id,
                owner_id=run.owner_id,
                auth_session_hash=run.auth_session_hash,
                claim_generation=self.claim_generation,
                definition=definition,
                arguments=arguments,
                destination_id=destination_id,
                destination_revision=profile.revision,
                scope=self.scope,
                original_fence=self.original_fence,
                multi_workspace_enabled=self.multi_workspace_enabled,
            )
        except TypeError:
            raise
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            return False

    async def authorize_internal_write(
        self,
        state: HarnessState,
        action_id: UUID,
        definition: Any,
        arguments: dict[str, Any],
    ) -> bool:
        """Revalidate session, claim, Chat link and evidence, then reserve the one internal write."""
        from modules.agents.approvals import reserve_effect_before_send

        try:
            run = await self._run_snapshot()
            destination_id, destination_revision = INTERNAL_DESTINATION
            if state.get("source_fences") and not await revalidate_native_output_fences(
                self.session_factory,
                self.decode_fences(state["source_fences"]),
                self.owner_principal(state, destination_id),
                destination_kind="remote",
                multi_workspace_enabled=self.multi_workspace_enabled,
            ):
                return False
            return await reserve_effect_before_send(
                self.session_factory,
                action_id=action_id,
                run_id=self.run_id,
                owner_id=run.owner_id,
                auth_session_hash=run.auth_session_hash,
                claim_generation=self.claim_generation,
                definition=definition,
                arguments=arguments,
                destination_id=destination_id,
                destination_revision=destination_revision,
                scope=self.scope,
                original_fence=self.original_fence,
                multi_workspace_enabled=self.multi_workspace_enabled,
            )
        except TypeError:
            raise
        except Exception:  # noqa: BLE001  # fail-closed boundary: any failure denies/degrades
            return False

    async def read_gateway_config(self, role: str | None = None) -> AIExecutionConfig:
        """Resolve current gateway settings in a short transaction after the run fences pass.

        With ``role`` the endpoint/key/destination are those of that role's provider.
        """
        await self._run_snapshot()
        async with self.session_factory() as session:
            config = await settings_public.get_ai_execution_config(
                session,
                self.settings,
                self.redis,
                scope=self.scope,
            )
        return settings_public.derive_for_role(config, role) if role else config

    async def authorize_remote_send(
        self,
        state: HarnessState,
        expected_revision: int | None = None,
        expected_destination: str | None = None,
    ) -> tuple[AIExecutionConfig, RequestPolicy, str, ModelMapping]:
        """Revalidate source-generation evidence, owner scope, cancellation, and remote consent."""
        self.assert_profile_checkpoint(state)
        await self._run_snapshot()
        if self.remaining_active() <= 0:
            raise RunLimitReached("Active execution budget exhausted")
        if state.get("source_fences"):
            principal = self.owner_principal(state, "")
            if not await revalidate_native_output_fences(
                self.session_factory,
                self.decode_fences(state["source_fences"]),
                principal,
                destination_kind="remote",
                multi_workspace_enabled=self.multi_workspace_enabled,
            ):
                raise RunCancelled("Source evidence is no longer authorized")
        config = await self.read_gateway_config()
        role = "chat"  # agents never choose a model: always the owner's chat role default
        config = settings_public.derive_for_role(config, role)
        mapping = config.defaults.resolve(role)
        destination = config.endpoint_destination_id or ""
        if self.profile_snapshot is not None:
            pinned = self.profile_snapshot.get("gateway")
            if not isinstance(pinned, dict) or (
                config.configuration_revision != pinned.get("configuration_revision")
                or config.gateway_identity != pinned.get("gateway_identity")
                or config.endpoint_destination_id != pinned.get("destination_id")
                or mapping is None
                or mapping.model != pinned.get("model")
                or mapping.version != pinned.get("model_version")
                or mapping.destination != pinned.get("model_destination")
                or config.privacy.model_dump(mode="json") != pinned.get("privacy")
            ):
                raise RunCancelled("Pinned profile gateway policy changed")
        if expected_revision is not None and config.configuration_revision != expected_revision:
            raise RunCancelled("Gateway settings changed during execution")
        if expected_destination is not None and destination != expected_destination:
            raise RunCancelled("Gateway destination changed during execution")
        policy = RequestPolicy(
            workspace_id=self.scope.workspace_id,
            actor_user_id=self.owner_id,
            membership_revision=self.scope.membership_revision,
            gateway_identity=config.gateway_identity,
            reasoning_allowed=config.privacy.allow_remote_reasoning,
            local_only=False,
            permitted_destinations=frozenset({destination} if destination else ()),
            reasoning_destinations=frozenset(config.privacy.reasoning_destinations),
            configuration_revision=config.configuration_revision,
        )
        if (
            config.endpoint_policy_denied
            or not may_send(
                policy, role, mapping, destination, bool(config.gateway_api_key), "tools"
            )
            or mapping is None
        ):
            raise PermissionError("Remote model execution is not authorized")
        return config, policy, role, mapping

    async def authorize_embedding_send(
        self,
        state: HarnessState,
        config: AIExecutionConfig,
        mapping: ModelMapping | None,
        policy: RequestPolicy,
        source_generations: dict[UUID, int],
    ) -> None:
        """Fence each hybrid-search embedding retry against current owner, source, and embedding policy.

        The search module supplies its original pre-await source snapshot and freshly reloaded
        embedding configuration. This check deliberately uses embedding policy rather than the
        agent's tool/chat capability, and propagates cancellation so hybrid search cannot silently
        downgrade a cancelled run into a lexical answer.
        """
        await self._run_snapshot()
        if self.remaining_active() <= 0:
            raise RunLimitReached("Active execution budget exhausted")
        if (
            not isinstance(source_generations, dict)
            or len(source_generations) > 100
            or any(
                not isinstance(source_id, UUID) or type(generation) is not int or generation < 1
                for source_id, generation in source_generations.items()
            )
        ):
            raise RunCancelled("Search source snapshot is invalid")
        current = await self.read_gateway_config("embedding")
        destination = current.endpoint_destination_id or "remote"
        principal = self.owner_principal(state, destination)
        fence = {"source_generations": source_generations, "records": []}
        if state.get("source_fences") and not await revalidate_native_output_fences(
            self.session_factory,
            self.decode_fences(state["source_fences"]),
            principal,
            destination_kind="remote",
            multi_workspace_enabled=self.multi_workspace_enabled,
        ):
            raise RunCancelled("Earlier source evidence is no longer authorized")
        if not await revalidate_native_output_fences(
            self.session_factory,
            fence,
            principal,
            destination_kind="remote",
            multi_workspace_enabled=self.multi_workspace_enabled,
        ):
            raise RunCancelled("Search source scope changed before embedding")
        current_mapping = current.defaults.resolve("embedding")
        if (
            current.configuration_revision != config.configuration_revision
            or current.endpoint_destination_id != config.endpoint_destination_id
            or current.gateway_identity != config.gateway_identity
            or current_mapping != mapping
            or policy.configuration_revision != config.configuration_revision
        ):
            raise RunCancelled("Embedding gateway settings changed before send")
        current_policy = RequestPolicy(
            workspace_id=self.scope.workspace_id,
            actor_user_id=self.owner_id,
            membership_revision=self.scope.membership_revision,
            gateway_identity=current.gateway_identity,
            embeddings_allowed=current.privacy.allow_remote_embeddings,
            local_only=False,
            permitted_destinations=frozenset(
                {current.endpoint_destination_id} if current.endpoint_destination_id else set()
            ),
            embedding_destinations=frozenset(current.privacy.embedding_destinations),
            configuration_revision=current.configuration_revision,
        )
        if current.endpoint_policy_denied or not may_send(
            current_policy,
            "embedding",
            current_mapping,
            destination,
            bool(current.gateway_api_key),
            "embeddings",
        ):
            raise PermissionError("Remote embedding execution is not authorized")

    def owner_principal(self, state: HarnessState, destination: str) -> ToolExecutionPrincipal:
        """Derive the owner principal and constrain profile runs to their saved source grant."""
        self.assert_profile_checkpoint(state)
        source_ids: frozenset[UUID] = frozenset()
        owner_all_sources = self.profile_snapshot is None
        if self.profile_snapshot is not None:
            try:
                raw_ids = self.profile_snapshot.get("source_ids", [])
                if not isinstance(raw_ids, list) or len(raw_ids) > 32:
                    raise ValueError("Profile source scope exceeds its bound")
                source_ids = frozenset(UUID(item) for item in raw_ids)
            except (ValueError, TypeError, AttributeError) as exc:
                raise RunCancelled("Profile source scope is invalid") from exc
        capabilities = {"source.read"}
        for name in self.allowed_tools:
            definition = self.registry.get_tool(name)
            if (
                definition is not None
                and (definition.risk == ToolRisk.READ_ONLY or is_internal_write(definition))
                and self.supports_definition(definition, self.tool_contracts.get(name, {}))
            ):
                capabilities.update(definition.permissions)
        if "webhook.send" in self.allowed_tools:
            capabilities.add("webhook.send")
        return ToolExecutionPrincipal(
            actor_id=f"owner:{self.owner_id}",
            scope=self.scope,
            is_owner=True,
            allowed_tools=self.allowed_tools,
            source_ids=source_ids,
            owner_all_sources=owner_all_sources,
            destinations=frozenset({destination}) if destination else frozenset(),
            capabilities=frozenset(capabilities),
        )

    @staticmethod
    def decode_fences(value: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the typed internal sink from bounded JSON primitives in a checkpoint."""
        generations = value.get("source_generations", {})
        records = value.get("records", [])
        if (
            not isinstance(generations, dict)
            or len(generations) > 100
            or not isinstance(records, list)
            or len(records) > 100
        ):
            raise RunCancelled("Checkpoint source fence is invalid")
        try:
            decoded_generations: dict[UUID, int] = {}
            for key, generation in generations.items():
                if type(key) is not str or type(generation) is not int or generation < 1:
                    raise ValueError("Invalid source generation")
                decoded_generations[UUID(key)] = generation
            decoded_records = []
            for record in records:
                if not isinstance(record, dict) or set(record) != {
                    "document_id",
                    "document_version_id",
                    "source_id",
                    "source_generation",
                    "chunk_id",
                }:
                    raise ValueError("Invalid result fence")
                decoded_records.append(
                    ToolOutputFence(
                        document_id=UUID(record["document_id"]),
                        document_version_id=UUID(record["document_version_id"]),
                        source_id=UUID(record["source_id"]),
                        source_generation=record["source_generation"],
                        chunk_id=UUID(record["chunk_id"])
                        if record["chunk_id"] is not None
                        else None,
                    )
                )
            return {
                "source_generations": decoded_generations,
                "records": decoded_records,
            }
        except (ValueError, TypeError) as exc:
            raise RunCancelled("Checkpoint source fence is invalid") from exc

    @staticmethod
    def encode_fences(value: dict[str, Any]) -> dict[str, Any]:
        """Convert internal UUID/pydantic fence identities to a bounded JSON-only checkpoint value."""
        generations = value.get("source_generations", {})
        records = value.get("records", [])
        if not isinstance(generations, dict) or not isinstance(records, list):
            raise RunCancelled("Tool output fence is invalid")
        return {
            "source_generations": {
                str(key): int(generation) for key, generation in generations.items()
            },
            "records": [
                item.model_dump(mode="json")
                if isinstance(item, ToolOutputFence)
                else ToolOutputFence.model_validate(item).model_dump(mode="json")
                for item in records
            ],
        }
