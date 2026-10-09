"""Per-source provider terms: owner acknowledgement, operator review, one shared eligibility check.

The owner declares deployment use and acknowledges a terms version (owner DTO has no review
fields, so approval cannot be mass-assigned). Review evidence is written only through
``record_operator_review`` under instance-operator admission. Catalog readiness and source
activation/collection both call ``decide``. No LLM and no network are involved. Every owner
write bumps ``terms_revision`` so a stale ``blocked_terms_revision`` gate cannot clear by accident.
"""

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.workspaces.schemas import Scope
from modules.connectors import provisioning
from modules.connectors import public as connectors
from modules.connectors.models import ConnectorProviderTerms
from modules.connectors.provider_specs import (
    TERMS_INELIGIBLE,
    FreeProviderSpec,
    OperatorReview,
    TermsDecision,
    evaluate_terms,
    get_provider_spec,
)
from modules.sources.schemas import ConnectorSource


class ProviderTermsAcknowledge(BaseModel):
    """Owner input only: the declared use and the terms version being acknowledged."""
    model_config = ConfigDict(extra="forbid")
    declared_use: Literal["personal", "noncommercial", "commercial", "unknown"]
    terms_version: str = Field(min_length=1, max_length=64, pattern=r"^\S(.*\S)?$")


class OperatorTermsReview(BaseModel):
    """Operator input: evidence for one exact terms version."""
    model_config = ConfigDict(extra="forbid")
    state: Literal["approved", "rejected"]
    allowed_use: Literal["personal", "noncommercial", "commercial"] | None = None
    evidence_ref: str = Field(min_length=1, max_length=512)
    reviewed_terms_version: str = Field(min_length=1, max_length=64)


class ProviderTermsRead(BaseModel):
    """Owner-visible state; includes the live eligibility decision, never review contents beyond state."""
    source_id: UUID
    provider_id: str
    terms_revision: int
    terms_url: str
    terms_version: str
    checked_on: date
    declared_use: str
    operator_review_state: str
    reviewed_allowed_use: str | None
    eligible: bool
    decision: str


def decide(spec: FreeProviderSpec, row: ConnectorProviderTerms | None) -> TermsDecision:
    """The single eligibility check; a missing row is denied by default."""
    if row is None:
        return evaluate_terms(spec, declared_use=None, acknowledged_terms_version=None)
    review = OperatorReview(
        row.operator_review_state, row.reviewed_allowed_use, row.review_evidence_ref, row.reviewed_terms_version,
    )  # type: ignore[arg-type]
    return evaluate_terms(
        spec, declared_use=row.declared_use, acknowledged_terms_version=row.terms_version, review=review,
    )


def _read(row: ConnectorProviderTerms, spec: FreeProviderSpec) -> ProviderTermsRead:
    decision = decide(spec, row)
    return ProviderTermsRead(
        source_id=row.source_id, provider_id=row.provider_id, terms_revision=row.terms_revision,
        terms_url=row.terms_url, terms_version=row.terms_version, checked_on=row.checked_on,
        declared_use=row.declared_use, operator_review_state=row.operator_review_state,
        reviewed_allowed_use=row.reviewed_allowed_use, eligible=decision.allowed, decision=decision.code,
    )


async def _terms_row(
    session: AsyncSession, source: ConnectorSource, *, lock: bool = False,
) -> ConnectorProviderTerms | None:
    """Workspace-predicated read; a row for another workspace's source id is never visible."""
    query = select(ConnectorProviderTerms).where(
        ConnectorProviderTerms.source_id == source.id, ConnectorProviderTerms.workspace_id == source.workspace_id,
    ).execution_options(populate_existing=True)
    return await session.scalar(query.with_for_update() if lock else query)


async def require_terms_eligible(session: AsyncSession, source: ConnectorSource) -> int | None:
    """Deny before any network or queueing; return the ``terms_revision`` to capture on the request.

    Providers outside the free catalog have no terms gate (None). Raises 409 ``provider_terms_ineligible``.
    """
    spec = get_provider_spec(source.provider)
    if spec is None:
        return None
    row = await _terms_row(session, source)
    if not decide(spec, row).allowed:
        raise HTTPException(status_code=409, detail=TERMS_INELIGIBLE)
    assert row is not None  # allowed implies an acknowledged row
    return row.terms_revision


async def get_terms(
    session: AsyncSession, source_id: UUID, *, scope: Scope, multi_workspace_enabled: bool,
) -> ProviderTermsRead | None:
    """Owner read of the source's terms state; None when the provider has none or nothing is recorded."""
    source = await connectors._read_scoped_source(
        session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    spec = get_provider_spec(source.provider)
    row = await _terms_row(session, source) if spec is not None else None
    return _read(row, spec) if row is not None and spec is not None else None


async def acknowledge_terms(
    session: AsyncSession, source_id: UUID, body: ProviderTermsAcknowledge,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> ProviderTermsRead:
    """Owner declares use and acknowledges a terms version; any change bumps ``terms_revision``."""
    actor = connectors._connector_actor(scope)
    try:
        source_fence, _, _ = await provisioning.lock_connector(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        source = await connectors._read_scoped_source(
            session, source_id, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
        if source_fence is None or source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        spec = get_provider_spec(source.provider)
        if spec is None:
            raise HTTPException(status_code=422, detail="Provider has no catalog terms")
        row = await _terms_row(session, source, lock=True)
        now = datetime.now(UTC)
        if row is None:
            row = ConnectorProviderTerms(
                source_id=source.id, workspace_id=source.workspace_id, provider_id=spec.id,
                terms_revision=1, terms_url=spec.terms_url, terms_version=body.terms_version,
                checked_on=date.fromisoformat(spec.checked_on), owner_acknowledged_at=now,
                owner_actor_user_id=actor, declared_use=body.declared_use, operator_review_state="pending",
            )
            session.add(row)
        elif (row.declared_use, row.terms_version) != (body.declared_use, body.terms_version):
            row.declared_use, row.terms_version = body.declared_use, body.terms_version
            row.terms_url, row.checked_on = spec.terms_url, date.fromisoformat(spec.checked_on)
            row.owner_acknowledged_at, row.owner_actor_user_id = now, actor
            row.terms_revision += 1
            # Review evidence stays recorded but no longer matches the new version/use (see decide()).
        await session.flush()
        result = _read(row, spec)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return result


async def record_operator_review(
    session: AsyncSession, *, workspace_id: UUID, source_id: UUID, reviewer_user_id: int,
    body: OperatorTermsReview, instance_operator: bool,
) -> ProviderTermsRead:
    """Operator-only: record review evidence for the acknowledged terms version."""
    if instance_operator is not True:
        raise PermissionError("Instance operator admission required")
    try:
        row = await session.scalar(
            select(ConnectorProviderTerms).where(
                ConnectorProviderTerms.source_id == source_id, ConnectorProviderTerms.workspace_id == workspace_id,
            ).with_for_update().execution_options(populate_existing=True))
        spec = get_provider_spec(row.provider_id) if row is not None else None
        if row is None or spec is None:
            raise HTTPException(status_code=404, detail="Provider terms not found")
        if body.reviewed_terms_version != row.terms_version:
            raise HTTPException(status_code=409, detail="Review does not match the acknowledged terms version")
        if body.state == "approved" and body.allowed_use is None:
            raise HTTPException(status_code=422, detail="Approval requires the allowed use")
        row.operator_review_state = body.state
        row.reviewer_user_id, row.reviewed_at = reviewer_user_id, datetime.now(UTC)
        row.reviewed_allowed_use = body.allowed_use if body.state == "approved" else None
        row.review_evidence_ref, row.reviewed_terms_version = body.evidence_ref, body.reviewed_terms_version
        row.terms_revision += 1
        await session.flush()
        result = _read(row, spec)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return result
