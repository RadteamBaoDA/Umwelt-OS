"""The ``collect_source`` action: queue one Topic watch collection for a rule's own Source.

The collection itself runs later in the connectors worker (plan, search, fetch, summarise); this
action only proves the owner's consent still matches the definition and requests the run, so an
unattended schedule never spends tokens on a definition the owner has not agreed to.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from core.errors import DomainError, RateLimitedError
from core.worker_context import worker_session_factory
from core.workspaces.schemas import Scope
from modules.automations._execution.runs import _mark
from modules.automations.schemas import topic_consent_hash
from modules.connectors import public as connectors


async def _collect_source(
    ctx: dict[str, Any],
    run: Any,
    row: Any,
    rev: Any,
    *,
    scope: Scope,
    multi_workspace_enabled: bool,
) -> str:
    """Request one collection for the action's Source and settle the action row; returns the outcome.

    Consent gate: the stored ``consent.hash`` must equal the hash of the revision's current watch,
    cron, timezone and model role; otherwise the action is ``skipped`` with ``consent_required`` and
    the run is dropped without any request. On success the action is ``succeeded`` with
    ``result_reference="collection:<request id>"`` (the collection result is settled by the connectors
    worker, not here). A rate-limited or unexpected failure is retried through ``_transient``; any other
    owner-module refusal (source inactive, revision changed) fails the action as ``collection_rejected``.
    Must be called after the locks are committed because the request command commits internally.
    """
    from modules.automations._execution.actions import _transient

    factory = worker_session_factory(ctx)
    spec = rev.actions[row.ordinal - 1]
    expected = topic_consent_hash(spec["watch"], rev.trigger, spec["model_role"])
    if (spec.get("consent") or {}).get("hash") != expected:
        await _mark(
            factory,
            run.id,
            row.ordinal,
            "skipped",
            "consent_required",
            None,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "dropped"
    try:
        async with factory() as session:
            request_id = await connectors.request_topic_watch_collection(
                session,
                UUID(spec["source_id"]),
                scope=scope,
                multi_workspace_enabled=multi_workspace_enabled,
            )
    except RateLimitedError:
        return await _transient(
            factory,
            run.id,
            row.ordinal,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    except DomainError:
        await _mark(
            factory,
            run.id,
            row.ordinal,
            "failed",
            "collection_rejected",
            None,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
        return "failed"
    except Exception:  # noqa: BLE001  # deliberate boundary: an unexpected failure is retried with bounds
        return await _transient(
            factory,
            run.id,
            row.ordinal,
            scope=scope,
            multi_workspace_enabled=multi_workspace_enabled,
        )
    await _mark(
        factory,
        run.id,
        row.ordinal,
        "succeeded",
        None,
        f"collection:{request_id}",
        scope=scope,
        multi_workspace_enabled=multi_workspace_enabled,
    )
    return "succeeded"
