"""Bounded Graphiti adapter facade; PostgreSQL remains the canonical evidence authority.

Implementation lives in the private ``_types``, ``_fingerprints``, ``_driver`` and ``_clients``
submodules; this module keeps ``TemporalGraph`` and re-exports every name other modules import.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from uuid import UUID

from core.model_gateway.client import ModelGateway
from modules.knowledge.temporal._clients import (
    _authorize,
    _completion_content,
    _make_clients,
)
from modules.knowledge.temporal._delete import (
    _apply_support_writes,
    _authorize_recoveries,
    _delete_and_verify_episode,
    _DeleteProgress,
    _inventory_episode,
    _plan_support_subtraction,
    _record_cleanup_intent,
    _recover_facts,
    _recover_nodes,
    _validate_delete_receipts,
    _verify_final_fact_inventory,
)
from modules.knowledge.temporal._driver import (
    _CONTROL_METADATA,
    _HEALTH_QUERY,
    _SETUP_METADATA,
    _SETUP_PREFIXES,
    _dispatch_scope,
    _DispatchScope,
    _make_driver_type,
    _positive_server_duration,
)
from modules.knowledge.temporal._fingerprints import (
    _authorize_cross_rebuild_absence,
    _canonical_json,
    _datetimes_to_strings,
    _delete_rebuild_node,
    _entity_state_fingerprint,
    _episode_state_fingerprint,
    _fact_recovery_fingerprint,
    _fact_state,
    _fact_states,
    _incident_ids_absent,
    _normalize_graph_values,
    _normalized_fact_attributes,
    _normalized_graph_timestamp,
    _receipt_bindings,
    _require_group,
    _safe_error,
    _strip_nul_bytes,
    _to_native,
)
from modules.knowledge.temporal._inspect import (
    _build_inspection,
    _check_inspection_partition,
    _inspection_scope,
    _read_inspection,
)
from modules.knowledge.temporal._intents import (
    _bulk_ids,
    _bulk_receipt,
    _fact_states_and_support,
    _intended_entity_states,
    _prior_entity_states,
)
from modules.knowledge.temporal._reconcile import (
    _fact_recovery_unavailable,
    _FactReconcileState,
    _node_recovery_unavailable,
    _NodeReconcileState,
    _reconcile_fact_action,
    _reconcile_node_action,
    _validate_fact_request,
    _validate_node_request,
)
from modules.knowledge.temporal._types import (
    _ABSENT_FACT_FINGERPRINT,
    _UUID,
    MAX_CANDIDATES,
    MAX_CAPTURE_BYTES,
    MAX_HISTORY,
    MAX_INPUT_BYTES,
    MAX_MODEL_CALLS,
    MAX_QUERY_BYTES,
    MAX_RECOVERY_WITNESSES,
    MAX_RESULTS,
    MAX_SUPPORT,
    OPERATION_SECONDS,
    QUERY_SECONDS,
    STARTUP_SECONDS,
    CandidateNodeReplacement,
    CanonicalEntityBinding,
    CanonicalNodeRecoveryAction,
    CanonicalNodeRecoveryOutcome,
    DeleteOutcome,
    DispatchOwnership,
    EpisodeRequest,
    EvidenceIdentity,
    ExactFactRecoveryAction,
    ExactFactRecoveryOutcome,
    ExactFactReplacement,
    ExactFactState,
    ExactFactSupport,
    ExactGraphLink,
    GraphConfiguration,
    GraphModelPolicy,
    GraphOperationError,
    GraphOperationUnknown,
    GraphReceiptInspection,
    GraphState,
    GraphWriteReceipt,
    GraphWriteState,
    OperationAuthorization,
    RecoveryReceiptAggregate,
    TemporalSearchResult,
    _authorize_receipt_witnesses,
    _validate_rebuild_mapping_ids,
)
from modules.knowledge.temporal.graph_fence import (
    PARTITION_FENCE_CONSTRAINT,
    PARTITION_FENCE_SCHEMA,
    require_partition_fence_schema,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_CANDIDATES",
    "MAX_CAPTURE_BYTES",
    "MAX_HISTORY",
    "MAX_INPUT_BYTES",
    "MAX_MODEL_CALLS",
    "MAX_QUERY_BYTES",
    "MAX_RECOVERY_WITNESSES",
    "MAX_RESULTS",
    "MAX_SUPPORT",
    "OPERATION_SECONDS",
    "QUERY_SECONDS",
    "STARTUP_SECONDS",
    "_ABSENT_FACT_FINGERPRINT",
    "_CONTROL_METADATA",
    "_HEALTH_QUERY",
    "_SETUP_METADATA",
    "_SETUP_PREFIXES",
    "_UUID",
    "CandidateNodeReplacement",
    "CanonicalEntityBinding",
    "CanonicalNodeRecoveryAction",
    "CanonicalNodeRecoveryOutcome",
    "DeleteOutcome",
    "DispatchOwnership",
    "EpisodeRequest",
    "EvidenceIdentity",
    "ExactFactRecoveryAction",
    "ExactFactRecoveryOutcome",
    "ExactFactReplacement",
    "ExactFactState",
    "ExactFactSupport",
    "ExactGraphLink",
    "GraphConfiguration",
    "GraphModelPolicy",
    "GraphOperationError",
    "GraphOperationUnknown",
    "GraphReceiptInspection",
    "GraphState",
    "GraphWriteReceipt",
    "GraphWriteState",
    "OperationAuthorization",
    "RecoveryReceiptAggregate",
    "TemporalGraph",
    "TemporalSearchResult",
    "_DispatchScope",
    "_authorize",
    "_authorize_cross_rebuild_absence",
    "_authorize_receipt_witnesses",
    "_canonical_json",
    "_completion_content",
    "_datetimes_to_strings",
    "_delete_rebuild_node",
    "_dispatch_scope",
    "_entity_state_fingerprint",
    "_episode_state_fingerprint",
    "_fact_recovery_fingerprint",
    "_fact_state",
    "_fact_states",
    "_incident_ids_absent",
    "_make_clients",
    "_make_driver_type",
    "_normalize_graph_values",
    "_normalized_fact_attributes",
    "_normalized_graph_timestamp",
    "_positive_server_duration",
    "_receipt_bindings",
    "_require_group",
    "_safe_error",
    "_strip_nul_bytes",
    "_to_native",
    "_validate_rebuild_mapping_ids",
]


class TemporalGraph:
    """Own disabled-by-default Graphiti lifecycle and fenced graph operations."""

    def __init__(self, configuration: GraphConfiguration) -> None:
        """Store a separate graph endpoint without importing packages or opening sockets."""
        self.configuration = configuration
        self.state = GraphState.DISABLED if not configuration.enabled else GraphState.UNCONFIGURED
        self._driver: Any | None = None
        self._graphiti_type: Any | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> GraphState:
        """Adopt a client after owned construction, indexes and erasure-schema verification.

        Startup budget bounds normal construction/index admission; timeout or
        cancellation still joins the noncancellable constructor thread and closes
        its returned driver before returning. That join has no hard wall-clock
        bound, so callers must retain heavy-work ownership through this await.
        """
        config = self.configuration
        if not config.enabled:
            await self.close()
            return self.state
        if not config.host or not config.password or not 1 <= config.port <= 65535:
            await self.close()
            return self.state
        async with self._lock:
            if self.state == GraphState.READY and self._driver is not None:
                return self.state
            # A failed health check can leave an adopted transport behind; dispose it before retrying.
            stale_driver = self._driver
            self._graphiti_type = None
            if stale_driver is not None:
                try:
                    await self._close_owned_driver(stale_driver)
                except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                    logger.warning("Graph retry cleanup incomplete category=%s", type(exc).__name__)
                    self._driver = stale_driver
                    return GraphState.UNAVAILABLE
            self.state = GraphState.UNAVAILABLE
            os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
            try:
                deadline = await self._construct_driver(config)
                self._graphiti_type = importlib.import_module("graphiti_core.graphiti").Graphiti
                async with asyncio.timeout_at(deadline):
                    assert self._driver is not None  # noqa: S101  # mypy narrowing invariant
                    await self._driver.build_indices_and_constraints(delete_existing=False)
                    await self._driver.control_query(PARTITION_FENCE_CONSTRAINT, {}, read=False)
                    require_partition_fence_schema(
                        await self._driver.control_query(PARTITION_FENCE_SCHEMA, {}, read=True),
                    )
                self.state = GraphState.READY
            except asyncio.CancelledError:
                self.state = GraphState.UNAVAILABLE
                await self._dispose_failed_driver("Graph initialization cleanup incomplete category=%s")
                raise
            except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                logger.warning("Graph initialization unavailable category=%s", type(exc).__name__)
                await self._dispose_failed_driver("Graph initialization cleanup incomplete category=%s")
                self.state = GraphState.UNAVAILABLE
            return self.state

    async def _dispose_failed_driver(self, message: str) -> None:
        """Close a driver left by a failed or cancelled initialization and forget the Graphiti type.

        Cleanup failures are logged (category only) and never raised, so the original
        failure keeps control flow.
        """
        failed_driver = self._driver
        self._graphiti_type = None
        if failed_driver is not None:
            try:
                await self._close_owned_driver(failed_driver)
            except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                logger.warning(message, type(exc).__name__)

    async def _construct_driver(self, config: GraphConfiguration) -> float:
        """Build the driver off-loop within the startup budget and adopt it; return the budget deadline.

        Cancellation or timeout cannot stop the constructor thread, so it is joined under the
        caller's capacity ownership and its returned driver adopted (for later disposal) before
        the interruption propagates.
        """
        driver_type = _make_driver_type()

        def construct() -> Any:
            """Build the bounded, non-retrying Neo4j client off-loop (no network I/O happens here)."""
            return driver_type(
                f"bolt://{config.host}:{config.port}", config.username, config.password, config.database,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_SECONDS
        construction = asyncio.create_task(asyncio.to_thread(construct))
        try:
            self._driver = await asyncio.wait_for(
                asyncio.shield(construction), timeout=max(0.0, deadline - loop.time())
            )
        except (TimeoutError, asyncio.CancelledError) as interrupted:
            # Cancellation cannot stop a Python thread. Join it under the
            # caller's capacity ownership, then dispose its returned driver.
            try:
                self._driver, cancelled_during_join = await self._join_owned_task(construction)
            except Exception as construction_error:
                if isinstance(interrupted, asyncio.CancelledError):
                    raise interrupted from construction_error
                raise
            if cancelled_during_join:
                raise asyncio.CancelledError from interrupted
            raise
        return deadline

    async def _join_owned_task(self, task: asyncio.Task[Any]) -> tuple[Any, bool]:
        """Join local work despite repeated caller cancellation, without orphan tasks.

        Return the result and whether the caller was cancelled while waiting.
        Callers adopt/close resources before propagating that cancellation. There
        is no hard thread join deadline; completed-task exceptions still surface.
        """
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:  # noqa: BLE001  # deliberate boundary: failure is recorded/handled so the loop or request continues
                # Retrieve the finished error below after preserving cancellation.
                break
        try:
            result = task.result()
        except Exception as exc:
            if cancelled:
                raise asyncio.CancelledError from exc
            raise
        return result, cancelled

    async def _close_owned_driver(self, driver: Any) -> None:
        """Await local driver cleanup to completion before releasing lifecycle ownership.

        Shield only protects the joined cleanup from caller cancellation. No
        background callback owns late cleanup and no elapsed budget permits an
        unfinished task to escape; propagate cancellation after successful close.
        A close exception retains the driver handle for a later explicit retry.
        """
        cleanup = asyncio.create_task(driver.close())
        _, cancelled = await self._join_owned_task(cleanup)
        if self._driver is driver:
            self._driver = None
        if cancelled:
            raise asyncio.CancelledError

    async def health(self) -> GraphState:
        """Check the adopted transport and join local failure cleanup under its lock.

        Health query budget does not bound cleanup completion; cancellation while
        closing is deferred until the owned close task finishes.
        """
        async with self._lock:
            if self.state != GraphState.READY or self._driver is None:
                return self.state
            try:
                async with asyncio.timeout(QUERY_SECONDS):
                    await self._driver.health_check()
                return GraphState.READY
            except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                logger.warning("Graph health unavailable category=%s", type(exc).__name__)
                self.state = GraphState.UNAVAILABLE
                failed_driver = self._driver
                if failed_driver is not None:
                    try:
                        await self._close_owned_driver(failed_driver)
                        self._graphiti_type = None
                    except Exception as cleanup_exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                        logger.warning("Graph health cleanup incomplete category=%s", type(cleanup_exc).__name__)
                return self.state

    async def close(self) -> None:
        """Serialize shutdown and await adopted local cleanup under caller ownership.

        Initialization holds the same lock until constructor/index work and failure
        cleanup finish. Close cannot return with a late constructor/cleanup task;
        cancellation propagates only after an active driver close is joined.
        Driver-close errors are reported and retain its handle for explicit retry.
        """
        async with self._lock:
            driver = self._driver
            self._graphiti_type = None
            self.state = GraphState.UNCONFIGURED if self.configuration.enabled else GraphState.DISABLED
            if driver is not None:
                try:
                    await self._close_owned_driver(driver)
                except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                    logger.warning("Graph close incomplete category=%s", type(exc).__name__)

    def _graphiti(self, context: OperationAuthorization, gateway: ModelGateway, driver: Any | None = None) -> Any:
        """Inject controlled clients and an operation driver so Graphiti cannot use provider defaults."""
        if self.state != GraphState.READY or self._driver is None or self._graphiti_type is None:
            raise GraphOperationError("graph_unavailable")
        llm, embedder, cross_encoder = _make_clients(context, gateway)
        return self._graphiti_type(
            graph_driver=driver or self._driver, llm_client=llm, embedder=embedder,
            cross_encoder=cross_encoder, max_coroutines=1,
        )

    @asynccontextmanager
    async def dispatch_session(self, context: OperationAuthorization) -> AsyncIterator[DispatchOwnership]:
        """Journal a unique dispatch token before all operation graph transactions.

        The owner callback must commit exact identity under its live operation
        lease; no graph command precedes that durable commit. Parent holds this
        scope around the complete awaited adapter operation. Every transaction in
        the scope carries the token as metadata and is never retried; cancellation
        leaves the remote outcome to exact cessation verification (terminating
        the tagged transactions). Setup probes contain no private evidence.
        Requires a positive server db.transaction.timeout (read-only probe; the
        adapter never changes settings) so abandoned transactions cannot run forever.
        Completion callback runs only after normal fully replied scope closure,
        updating this exact dispatch rather than any earlier unknown one.
        """
        if (self.state != GraphState.READY or self._driver is None
                or context.record_dispatch_ownership is None
                or _dispatch_scope.get() is not None):
            raise GraphOperationError("graph_dispatch_ownership_required")
        context_token = None
        scope: _DispatchScope | None = None
        replied = False
        try:
            async with asyncio.timeout(QUERY_SECONDS):
                rows = await self._driver.control_query(
                    "SHOW SETTINGS YIELD name, value WHERE name = 'db.transaction.timeout'", {}, read=True,
                )
                if len(rows) != 1 or not _positive_server_duration(rows[0].get("value")):
                    raise GraphOperationError("graph_write_timeout_unconfigured")
                # Neo4j exposes no comparable server/client identity; mint an unguessable one.
                ownership = DispatchOwnership(
                    context.operation_id, context.group_id,
                    secrets.token_hex(20), secrets.randbelow(2**63 - 2) + 1,
                )
                await context.record_dispatch_ownership(ownership)
            scope = _DispatchScope(ownership)
            context_token = _dispatch_scope.set(scope)
            yield ownership
            replied = not scope.poisoned and scope.pending == 0
        except GraphOperationError:
            raise
        except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
            raise _safe_error(exc) from None
        finally:
            if context_token is not None:
                # Late tasks must not reuse a closed scope; closing is not proof of remote completion.
                if scope is not None:
                    scope.poisoned = True
                _dispatch_scope.reset(context_token)
        if replied and context.record_dispatch_completion is not None:
            # Exact reply proof closes only this dispatch journal row.
            async with asyncio.timeout(QUERY_SECONDS):
                await context.record_dispatch_completion(ownership)

    async def verify_dispatch_cessation(
        self, ownership: DispatchOwnership, context: OperationAuthorization,
    ) -> bool:
        """Terminate exactly an owner-journaled dispatch's transactions and prove cessation.

        Owner callback must compare stored ownership, current recovery lease and
        stopped local tasks before any TERMINATE; it is called again immediately
        before terminating. Only transactions whose metadata carries the journaled
        token are listed and terminated, then SHOW is polled (bounded by
        QUERY_SECONDS) until none remain, so no tagged statement can still commit.
        A restarted server has no such transactions, which also proves cessation.
        No graph/model context is loaded; this says nothing about success,
        requiring exact-ID reconciliation.
        """
        if (self.state != GraphState.READY or self._driver is None
                or ownership.group_id != context.group_id
                or ownership.operation_id != context.operation_id
                or context.authorize_dispatch_cessation is None):
            raise GraphOperationError("graph_dispatch_cessation_denied")
        await context.authorize_dispatch_cessation(ownership)
        show = ("SHOW TRANSACTIONS YIELD transactionId, metaData "
                "WHERE metaData.umwelt_dispatch = $token RETURN transactionId")
        token = _DispatchScope(ownership).token
        driver = self._driver
        try:
            async with asyncio.timeout(QUERY_SECONDS):
                rows = await driver.control_query(show, {"token": token}, read=True)
                if not rows:
                    return True
                ids = [str(row["transactionId"]) for row in rows[:MAX_SUPPORT]]
                # Re-authorize right before the destructive command: the lease may have changed.
                await context.authorize_dispatch_cessation(ownership)
                await driver.control_query("TERMINATE TRANSACTIONS $ids", {"ids": ids}, read=False)
                while await driver.control_query(show, {"token": token}, read=True):  # noqa: ASYNC110  # polls the graph driver state; there is no event to await
                    await asyncio.sleep(0.1)
                return True
        except GraphOperationError:
            raise
        except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
            raise _safe_error(exc) from None

    async def _partition_driver(
        self, group_id: str,
        write_intent_hook: Callable[..., Awaitable[None]] | None = None,
    ) -> Any:
        """Return a per-operation driver copy only inside a journaled dispatch scope.

        The copy shares the single client and database; partition isolation comes
        from group_id predicates in every query, not from the database. Indexes are
        built once in initialize. Receipt hooks retain their original ordering.
        """
        if self._driver is None:
            raise GraphOperationError("graph_unavailable")
        scope = _dispatch_scope.get()
        if scope is None or scope.ownership.group_id != group_id:
            raise GraphOperationError("graph_dispatch_ownership_required")
        driver = copy.copy(self._driver)
        driver._write_intent_hook = write_intent_hook
        return driver

    async def _record_bulk_write_intent(
        self, request: EpisodeRequest, context: OperationAuthorization,
        shell_receipt: GraphWriteReceipt, latest_receipt: list[GraphWriteReceipt | None],
        episodes: Sequence[Any], mentions: Sequence[Any], nodes: Sequence[Any],
        facts: Sequence[Any], driver: Any, *,
        final_node_payloads: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Journal IDs first, then final hydrated node state before any captured bulk write dispatch.

        Raises ``GraphOperationError`` for invalid/ambiguous ids, changed prior node
        state between the ID and hydrated receipts, invalid support, or when the
        partition revalidation or owner journal write fails. On success
        ``latest_receipt[0]`` holds the new receipt; hydrated receipts are followed
        by a partition recheck immediately before captured calls flush.
        """
        ids = _bulk_ids(request, episodes, mentions, nodes, facts)
        entity_ids, _, fact_ids = ids
        prior_entity_states = await _prior_entity_states(driver, request, context, entity_ids)
        previous_receipt = latest_receipt[0]
        if (final_node_payloads is not None and previous_receipt is not None
                and previous_receipt.phase == "bulk_ids_intent"
                and previous_receipt.prior_entity_state_fingerprints != prior_entity_states):
            raise GraphOperationError("graph_write_intent_prior_state_changed")
        intended_entity_states: tuple[tuple[str, str | None], ...] = ()
        if final_node_payloads is not None:
            intended_entity_states = _intended_entity_states(context, entity_ids, final_node_payloads)
        states, support = await _fact_states_and_support(driver, request, context, facts, fact_ids)
        receipt = _bulk_receipt(
            request, context, shell_receipt, episodes, ids, prior_entity_states,
            intended_entity_states, states, support, final_node_payloads is not None,
        )
        try:
            await context.validate_partition("upsert", str(request.episode_id))
            await context.record_write_intent(receipt)
        except Exception:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
            raise GraphOperationError("graph_write_intent_unavailable") from None
        latest_receipt[0] = receipt
        if final_node_payloads is not None:
            # Recheck after durable intent and immediately before SafeSession flushes captured calls.
            await context.validate_partition("upsert", str(request.episode_id))

    async def _validate_partition(
        self, context: OperationAuthorization,
        operation: Literal["search", "upsert", "delete", "reconcile"], episode_id: str | None = None,
    ) -> None:
        """Prove owner readiness and inventory, allowing only this authorized target to be absent."""
        # Owner validation checks full canonical support and cleanup state, not recent history.
        await context.validate_partition(operation, episode_id)
        driver = await self._partition_driver(context.group_id)
        try:
            records, _, _ = await driver.execute_query(
                "MATCH (episode:Episodic {group_id: $group_id}) "
                "RETURN episode.uuid AS episode_id ORDER BY episode.uuid LIMIT $limit",
                group_id=context.group_id,
                limit=MAX_SUPPORT + 1,
            )
        except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
            raise _safe_error(exc) from None
        if not isinstance(records, list) or len(records) > MAX_SUPPORT:
            raise GraphOperationError("graph_partition_inventory_invalid")
        graph_episode_ids = {row.get("episode_id") for row in records if isinstance(row, dict)}
        expected_episode_ids = set(context.partition_episode_ids)
        inventory_matches = graph_episode_ids == expected_episode_ids
        if operation in {"upsert", "delete", "reconcile"} and episode_id is not None:
            inventory_matches = inventory_matches or (
                episode_id in expected_episode_ids
                and graph_episode_ids == expected_episode_ids - {episode_id}
            )
        if len(graph_episode_ids) != len(records) or not inventory_matches:
            raise GraphOperationError("graph_partition_inventory_mismatch")

    async def _ensure_canonical_nodes(
        self, request: EpisodeRequest, context: OperationAuthorization, driver: Any,
        write_started: list[bool], latest_receipt: list[GraphWriteReceipt | None],
    ) -> tuple[str, ...]:
        """Journal and materialize source-proved canonical seeds, then verify each exact UUID."""
        from graphiti_core.nodes import EntityNode

        verified: list[str] = []
        for binding in request.canonical_bindings:
            try:
                current = await EntityNode.get_by_uuid(driver, binding.graph_entity_uuid)
            except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                if type(exc).__name__ != "NodeNotFoundError":
                    raise _safe_error(exc) from None
                current = None
            if current is not None:
                if current.group_id != request.group_id:
                    raise GraphOperationError("graph_canonical_entity_partition_mismatch")
                await current.load_name_embedding(driver)
                if (binding.prior_node_state_fingerprint is None
                        or _entity_state_fingerprint(current, context.embedding.dimensions)
                        != binding.prior_node_state_fingerprint):
                    raise GraphOperationError("graph_canonical_entity_state_changed")
                verified.append(binding.graph_entity_uuid)
                continue
            if (binding.node_name is None or not binding.node_name.strip()
                    or not any(field == "name" for field, _, _ in binding.field_support)):
                raise GraphOperationError("graph_canonical_entity_seed_unavailable")
            entity = EntityNode(
                uuid=binding.graph_entity_uuid,
                group_id=request.group_id,
                name=binding.node_name,
                summary=binding.node_summary or "",
                attributes={},
                labels=["Entity"],
            )
            intended = _entity_state_fingerprint(
                entity, context.embedding.dimensions, writer_intended=True,
            )
            receipt = GraphWriteReceipt(
                operation_id=context.operation_id,
                lease_token=context.lease_token,
                episode_id=request.episode_id,
                group_id=request.group_id,
                mapping_revision=request.mapping_revision,
                desired_support_digest=intended,
                phase="canonical_node_write_intent",
                entity_ids=(binding.graph_entity_uuid,),
                intended_entity_state_fingerprints=((binding.graph_entity_uuid, intended),),
                prior_entity_state_fingerprints=((binding.graph_entity_uuid, False, None),),
                canonical_bindings=_receipt_bindings((binding,)),
            )
            try:
                await context.record_write_intent(receipt)
            except Exception:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                raise GraphOperationError("graph_write_intent_unavailable") from None
            latest_receipt[0] = receipt
            try:
                write_started[0] = True
                await entity.save(driver)
                saved = await EntityNode.get_by_uuid(driver, binding.graph_entity_uuid)
                await saved.load_name_embedding(driver)
            except (TimeoutError, asyncio.CancelledError):
                raise GraphOperationUnknown("graph_canonical_entity_outcome_unknown", receipt) from None
            except Exception:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                raise GraphOperationUnknown("graph_canonical_entity_outcome_unknown", receipt) from None
            if (saved.group_id != request.group_id
                    or _entity_state_fingerprint(saved, context.embedding.dimensions) != intended):
                raise GraphOperationUnknown("graph_canonical_entity_outcome_unknown", receipt) from None
            verified.append(binding.graph_entity_uuid)
        return tuple(verified)

    async def upsert_episode(
        self, request: EpisodeRequest, context: OperationAuthorization, gateway: ModelGateway,
    ) -> dict[str, Any]:
        """Upsert a stable episode under its full source/evidence fence and durable write receipts.

        T3 commits mapping and intent first. Graph writes outside the bulk
        transaction span multiple non-atomic statements; a timeout returns unknown and must be reconciled
        by exact UUID before retrying.
        """
        if (request.group_id != context.group_id
                or str(request.episode_id) not in context.partition_episode_ids
                or not set(request.evidence) <= set(context.evidence)):
            raise GraphOperationError("graph_episode_outside_authorized_support")
        graph_write_started = [False]
        latest_receipt: list[GraphWriteReceipt | None] = [None]
        shell_receipt: GraphWriteReceipt | None = None
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await self._validate_partition(context, "upsert", str(request.episode_id))
                    await context.authorize("reasoning")
                    from graphiti_core.nodes import EpisodeType, EpisodicNode

                    driver = await self._partition_driver(request.group_id)
                    verified_binding_ids = await self._ensure_canonical_nodes(
                        request, context, driver, graph_write_started, latest_receipt,
                    )

                    try:
                        existing = await EpisodicNode.get_by_uuid(driver, str(request.episode_id))
                    except Exception as exc:
                        if type(exc).__name__ != "NodeNotFoundError":
                            raise
                    else:
                        if existing.group_id != request.group_id:
                            raise GraphOperationError("graph_episode_id_partition_conflict")
                        # Graphiti writes several non-atomic statements; never re-extract an existing ID.
                        raise GraphOperationUnknown("graph_episode_existing_requires_reconciliation")
                    episode = EpisodicNode(
                        uuid=str(request.episode_id), name=request.name, group_id=request.group_id,
                        source=EpisodeType.text,
                        source_description="BBD-OS evidence-backed temporal episode",
                        content=request.content, valid_at=request.reference_time.astimezone(UTC),
                        created_at=datetime.now(UTC), entity_edges=[],
                    )
                    # Stable-ID shell exists before Graphiti extraction so partial writes remain identifiable.
                    shell_receipt = GraphWriteReceipt(
                        operation_id=context.operation_id,
                        lease_token=context.lease_token,
                        episode_id=request.episode_id,
                        group_id=request.group_id,
                        mapping_revision=request.mapping_revision,
                        desired_support_digest=hashlib.sha256(b"[]").hexdigest(),
                        phase="shell_write_intent",
                        episode_state_fingerprint=_episode_state_fingerprint(episode),
                        canonical_bindings=_receipt_bindings(request.canonical_bindings),
                    )
                    try:
                        await context.record_write_intent(shell_receipt)
                    except Exception:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                        raise GraphOperationError("graph_write_intent_unavailable") from None
                    latest_receipt[0] = shell_receipt
                    driver._write_intent_hook = partial(
                        self._record_bulk_write_intent, request, context, shell_receipt, latest_receipt,
                    )
                    graphiti = self._graphiti(context, gateway, driver)
                    graph_write_started[0] = True
                    await episode.save(driver)
                    result = await graphiti.add_episode(
                        name=request.name, episode_body=request.content, source=EpisodeType.text,
                        source_description="BBD-OS evidence-backed temporal episode",
                        reference_time=request.reference_time.astimezone(UTC),
                        group_id=request.group_id, uuid=str(request.episode_id),
                        previous_episode_uuids=list(context.history_episode_ids),
                    )
                    return {
                        "episode_id": str(request.episode_id), "mapping_revision": request.mapping_revision,
                        "canonical_entity_ids": request.canonical_entity_ids,
                        "verified_canonical_bindings": tuple(
                            (item.canonical_entity_id, item.graph_entity_uuid,
                             item.canonical_revision, item.mapping_revision)
                            for item in request.canonical_bindings
                        ),
                        "verified_canonical_graph_entity_ids": verified_binding_ids,
                        "graph_entity_ids": tuple(node.uuid for node in getattr(result, "nodes", [])),
                        "outcome": "succeeded",
                    }
            except (TimeoutError, asyncio.CancelledError):
                raise GraphOperationUnknown("graph_episode_outcome_unknown", latest_receipt[0]) from None
            except GraphOperationUnknown as exc:
                raise GraphOperationUnknown(str(exc), exc.receipt or latest_receipt[0]) from None
            except GraphOperationError:
                if graph_write_started[0]:
                    raise GraphOperationUnknown("graph_episode_outcome_unknown", latest_receipt[0]) from None
                raise
            except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                error = _safe_error(exc)
                if graph_write_started[0] and isinstance(error, GraphOperationUnknown):
                    raise GraphOperationUnknown(str(error), latest_receipt[0]) from None
                raise error from None

    async def search_at(
        self, query: str, valid_at: datetime, context: OperationAuthorization,
        gateway: ModelGateway, *, limit: int = MAX_RESULTS,
    ) -> list[TemporalSearchResult]:
        """Search one source-generation partition and canonicalize every graph result."""
        if (not query.strip() or len(query.encode()) > MAX_QUERY_BYTES or valid_at.tzinfo is None
                or not 1 <= limit <= MAX_RESULTS):
            raise ValueError("Temporal graph search is invalid or exceeds its bounds")
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await self._validate_partition(context, "search")
                    if not context.partition_episode_ids:
                        return []
                    await context.authorize("embedding")
                    driver = await self._partition_driver(context.group_id)
                    graphiti = self._graphiti(context, gateway, driver)
                    from graphiti_core.search.search_filters import (
                        ComparisonOperator,
                        DateFilter,
                        SearchFilters,
                    )

                    instant = valid_at.astimezone(UTC)

                    raw = await graphiti.search(
                        query, group_ids=[context.group_id], num_results=limit,
                        search_filter=SearchFilters(
                            valid_at=[[
                                DateFilter(
                                    date=instant,
                                    comparison_operator=ComparisonOperator.less_than_equal,
                                ),
                            ]],
                            invalid_at=[[
                                DateFilter(
                                    date=instant,
                                    comparison_operator=ComparisonOperator.greater_than,
                                ),
                            ], [
                                DateFilter(comparison_operator=ComparisonOperator.is_null),
                            ]],
                        ),
                    )
                    if len(raw) > MAX_CANDIDATES:
                        raise GraphOperationError("graph_search_candidate_limit")
                    canonical = await context.canonicalize(raw)
                    allowed_episode_ids = set(context.partition_episode_ids)
                    if (len(canonical) > limit or any(
                        not item.episode_ids or not set(item.episode_ids) <= allowed_episode_ids
                        for item in canonical
                    )):
                        raise GraphOperationError("graph_result_outside_authorized_partition")
                    return canonical
            except (TimeoutError, asyncio.CancelledError):
                raise GraphOperationUnknown("graph_search_outcome_unknown") from None
            except GraphOperationError:
                raise
            except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                raise _safe_error(exc) from None

    async def inspect_write_receipt(
        self, receipt: GraphWriteReceipt | tuple[GraphWriteReceipt, ...],
        context: OperationAuthorization,
        actions: tuple[CanonicalNodeRecoveryAction, ...] = (),
    ) -> GraphReceiptInspection:
        """Inspect one normal receipt or an owner-approved bounded recovery aggregate by exact IDs.

        Read-only. Raises ``GraphOperationError`` for receipts outside the authorized
        partition/lease, an unavailable graph, or an inventory mismatch; unexpected
        dependency failures inside the fence are re-mapped to a sanitized error.
        """
        scope = _inspection_scope(receipt, context, actions)
        if self.state != GraphState.READY or self._driver is None:
            raise GraphOperationError("graph_unavailable")
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    await _authorize_receipt_witnesses(context, scope.receipts, "node")
                    if scope.node_recovery:
                        assert context.authorize_node_recovery is not None  # noqa: S101  # mypy narrowing invariant
                        await context.authorize_node_recovery(scope.receipts, actions)
                    await self._validate_partition(context, "reconcile", scope.episode_id)
                    driver = await self._partition_driver(context.group_id)
                    read = await _read_inspection(driver, context, scope)
                    _check_inspection_partition(context, scope, read)
                    return _build_inspection(context, read)
            except GraphOperationError:
                raise
            except Exception as exc:  # noqa: BLE001  # error boundary: re-mapped to a sanitized error
                raise _safe_error(exc) from None

    async def reconcile_canonical_nodes(
        self, receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[CanonicalNodeRecoveryAction, ...], context: OperationAuthorization,
    ) -> CanonicalNodeRecoveryOutcome:
        """Apply owner-authorized node recovery after exact inventory and fingerprint checks.

        Receipt witnesses and actions have independent bounds; their physical
        effect union remains capped at 100 IDs. Typed rebuild deletion requires
        owner-committed dependent rebuild schedules and the complete exact incident
        inventory. Missing objects require own absence evidence or certified
        cross-operation absence, journaled under this operation's live lease.

        Raises ``GraphOperationError`` for unavailable/out-of-partition requests and
        for owner-callback ``GraphOperationError``; timeouts and other failures
        return a not-converged outcome with every action marked unresolved.
        """
        if (self.state != GraphState.READY or self._driver is None
                or _node_recovery_unavailable(context, receipts, actions)):
            raise GraphOperationError("graph_node_recovery_unavailable")
        episode_id = _validate_node_request(context, receipts, actions)
        action_ids = {item.graph_entity_uuid for item in actions}
        state = _NodeReconcileState(retained_receipts=list(receipts))
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    return await self._reconcile_nodes_fenced(receipts, actions, context, episode_id, state)
            except GraphOperationError:
                raise
            except (TimeoutError, asyncio.CancelledError):
                return state.unknown_outcome(action_ids)
            except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                logger.warning("Graph canonical-node recovery incomplete category=%s", type(exc).__name__)
                return state.unknown_outcome(action_ids)

    async def _reconcile_nodes_fenced(
        self, receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[CanonicalNodeRecoveryAction, ...], context: OperationAuthorization,
        episode_id: str, state: _NodeReconcileState,
    ) -> CanonicalNodeRecoveryOutcome:
        """Authorize, validate the partition and reconcile each node action inside the fence and timeout."""
        await _authorize_receipt_witnesses(context, receipts, "node")
        assert context.authorize_node_recovery is not None  # noqa: S101  # mypy narrowing invariant
        await context.authorize_node_recovery(receipts, actions)
        await self._validate_partition(context, "reconcile", episode_id)
        driver = await self._partition_driver(context.group_id)
        for action in actions:
            await _reconcile_node_action(driver, context, receipts, actions, action, episode_id, state)
        return state.final_outcome()

    async def reconcile_fact_recovery(
        self, receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[ExactFactRecoveryAction, ...], context: OperationAuthorization,
    ) -> ExactFactRecoveryOutcome:
        """Apply fresh owner-authorized fact corrections with exact vector-aware pre/post fences.

        Bound original receipt witnesses independently from the 100-action/effect
        cap and certify any complete journal aggregate before graph inspection.
        Typed rebuild deletion requires owner-committed dependent rebuild schedules.
        Exact absence is resolved before endpoint presence, allowing completed node
        rebuilds to remove facts while preserving original endpoint evidence.

        Raises ``GraphOperationError`` for unavailable/invalid requests and for
        owner-callback ``GraphOperationError``; timeouts and other failures return
        a not-converged outcome with every action marked unresolved.
        """
        if (self.state != GraphState.READY or self._driver is None
                or _fact_recovery_unavailable(context, receipts, actions)):
            raise GraphOperationError("graph_fact_recovery_unavailable")
        episode_id = _validate_fact_request(context, receipts, actions)
        action_ids = {item.fact_id for item in actions}
        state = _FactReconcileState(retained_receipts=list(receipts))
        async with context.fence():
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    return await self._reconcile_facts_fenced(receipts, actions, context, episode_id, state)
            except GraphOperationError:
                raise
            except (TimeoutError, asyncio.CancelledError):
                return state.unknown_outcome(action_ids)
            except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
                logger.warning("Graph fact recovery incomplete category=%s", type(exc).__name__)
                return state.unknown_outcome(action_ids)

    async def _reconcile_facts_fenced(
        self, receipts: tuple[GraphWriteReceipt, ...],
        actions: tuple[ExactFactRecoveryAction, ...], context: OperationAuthorization,
        episode_id: str, state: _FactReconcileState,
    ) -> ExactFactRecoveryOutcome:
        """Authorize, validate the partition and reconcile each fact action inside the fence and timeout."""
        await _authorize_receipt_witnesses(context, receipts, "fact")
        assert context.authorize_fact_recovery is not None  # noqa: S101  # mypy narrowing invariant
        await context.authorize_fact_recovery(receipts, actions)
        await self._validate_partition(context, "reconcile", episode_id)
        driver = await self._partition_driver(context.group_id)
        for action in actions:
            await _reconcile_fact_action(driver, context, receipts, actions, action, episode_id, state)
        return state.final_outcome()

    async def delete_episode(
        self, episode_id: UUID, context: OperationAuthorization,
        known_owned: bool, tombstone_recorded: bool,
    ) -> DeleteOutcome:
        """Remove a tombstoned episode and prove current authorized recovery or return exact pending IDs.

        The caller holds the source/document fence. Receipt history and detached
        recovery actions must match its live lease; graph reads remain partitioned,
        vector-aware and exact-ID bounded. Episode removal is reported separately
        from fresh fact recomputation and node recovery completion. Typed rebuild
        deletion requires exact incident absence; cross-operation absence is
        accepted only after readback and a durable owner proof, then journaled as
        this operation's actual observation.

        Raises ``GraphOperationError`` only for the ownership precondition; every
        later failure (including timeout and cancellation inside the operation
        window) returns a partial ``unknown`` outcome carrying the ids gathered so
        far through ``_DeleteProgress``.
        """
        if (not known_owned or not tombstone_recorded or not _UUID.fullmatch(str(episode_id))
                or not context.partition_episode_ids
                or str(episode_id) not in context.partition_episode_ids):
            raise GraphOperationError("graph_delete_requires_owned_tombstone")
        if self.state != GraphState.READY or self._driver is None:
            return DeleteOutcome(str(episode_id), False, (), (), "unknown")
        progress = _DeleteProgress()
        try:
            async with context.fence():
                async with asyncio.timeout(OPERATION_SECONDS):
                    return await self._delete_episode_fenced(episode_id, context, progress)
        except (TimeoutError, asyncio.CancelledError, GraphOperationError):
            return progress.partial_outcome(episode_id)
        except Exception as exc:  # noqa: BLE001  # boundary: failure logged, caller degrades safely
            logger.warning("Graph deletion incomplete category=%s", type(exc).__name__)
            return progress.partial_outcome(episode_id)

    async def _delete_episode_fenced(
        self, episode_id: UUID, context: OperationAuthorization, progress: _DeleteProgress,
    ) -> DeleteOutcome:
        """Run the ordered delete steps inside the caller's fence and timeout.

        Order: receipt validation -> recovery authorization -> graph inventory ->
        pure support plan -> intent journaled -> per-edge recheck/write/read-back ->
        episode removal and read-back -> node/fact recovery proofs. ``progress`` is
        updated as ids become known so the caller can report a partial outcome.
        """
        view = await _validate_delete_receipts(context, episode_id)
        if view is None:
            return DeleteOutcome(str(episode_id), False, (), (), "unknown")
        progress.recovery_ids = view.known_entity_ids
        await _authorize_recoveries(context, view.receipts, view.node_actions, view.fact_actions)
        validate = partial(self._validate_partition, context, "delete", str(episode_id))
        await validate()
        driver = await self._partition_driver(context.group_id)
        inventory = await _inventory_episode(driver, context, episode_id, view, progress)
        plan = _plan_support_subtraction(inventory, view, episode_id, context.embedding.dimensions)
        progress.shared_ids = plan.shared_ids
        pending_fact_ids = set(plan.shared_ids)
        pending_fact_ids.update(item.fact_id for item in view.fact_actions)
        pending_node_ids = set(view.known_entity_ids)
        receipts = await _record_cleanup_intent(context, episode_id, view, inventory, plan, validate)
        if receipts is None:
            return progress.partial_outcome(episode_id)
        await _apply_support_writes(driver, context, plan, validate)
        verification = await _delete_and_verify_episode(
            driver, context, episode_id, inventory.episode, receipts, view.known_mention_ids, validate,
        )
        await validate()
        await _authorize_recoveries(context, receipts, view.node_actions, view.fact_actions)
        pending_node_ids.difference_update(
            await _recover_nodes(driver, context, episode_id, view, receipts),
        )
        final = await _verify_final_fact_inventory(driver, context, episode_id, view, receipts, inventory)
        pending_fact_ids.difference_update(_recover_facts(context, episode_id, view.fact_actions, final))
        episode_cleanup_verified = (
            verification.episode_absent and not verification.remaining_mentions
            and not verification.reverse_support and final.matches
        )
        progress.recovery_ids = tuple(sorted(pending_node_ids))
        progress.stale_ids = tuple(sorted(pending_fact_ids & plan.stale))
        progress.shared_ids = tuple(sorted(pending_fact_ids))
        outcome: Literal["succeeded", "unknown"] = (
            "succeeded" if episode_cleanup_verified and not pending_fact_ids and not pending_node_ids
            else "unknown"
        )
        return DeleteOutcome(
            str(episode_id), episode_cleanup_verified, progress.affected_ids, progress.shared_ids,
            outcome, progress.stale_ids, progress.recovery_ids,
        )
