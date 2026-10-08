"""Private SQLAlchemy persistence models for dashboard configuration and layout."""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from core.database import Base


class Dashboard(Base):
    """Own a named dashboard and one safe-integer revision for all structural edits."""

    __tablename__ = "dashboards"
    __table_args__ = (
        CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_dashboards_revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DashboardGroup(Base):
    """Group instances within one dashboard while exposing a composite target for safe FKs."""

    __tablename__ = "dashboard_groups"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_dashboard_groups_position"),
        UniqueConstraint("id", "dashboard_id", name="uq_dashboard_groups_id_dashboard"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    dashboard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class GadgetDefinition(Base):
    """Persist reusable bounded selectors and renderer configuration without source content."""

    __tablename__ = "gadget_definitions"
    __table_args__ = (
        CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_gadget_definitions_revision"),
        CheckConstraint("jsonb_typeof(source_ids) = 'array'", name="ck_gadget_definitions_source_ids_array"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owner.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    renderer: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    highlight_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GadgetHighlightSuppression(Base):
    """Notification keys suppressed by rule delivery policy; never re-evaluated (BM-34)."""

    __tablename__ = "gadget_highlight_suppression"

    definition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gadget_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GadgetHighlightProgress(Base):
    """Durable immutable-version scan cursor bound to one definition revision and rule set."""

    __tablename__ = "gadget_highlight_progress"
    __table_args__ = (
        CheckConstraint("definition_revision >= 1", name="ck_gadget_highlight_progress_revision"),
        CheckConstraint(
            "(cursor_created_at IS NULL) = (cursor_version_id IS NULL)",
            name="ck_gadget_highlight_progress_cursor_pair",
        ),
    )

    definition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gadget_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    definition_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rules_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    # rule_id -> ISO instant of the last notification; reset when the rules fingerprint changes.
    rule_last_notified: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GadgetInstance(Base):
    """Place one reusable definition into a dashboard-owned group with a local title/order."""

    __tablename__ = "gadget_instances"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_gadget_instances_position"),
        UniqueConstraint("id", "dashboard_id", name="uq_gadget_instances_id_dashboard"),
        ForeignKeyConstraint(
            ["group_id", "dashboard_id"],
            ["dashboard_groups.id", "dashboard_groups.dashboard_id"],
            ondelete="CASCADE",
            name="fk_gadget_instances_group_dashboard",
        ),
        ForeignKeyConstraint(
            ["definition_id"], ["gadget_definitions.id"], ondelete="RESTRICT",
            name="fk_gadget_instances_definition",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    dashboard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    definition_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class DashboardLayout(Base):
    """Persist independent desktop and mobile column counts under a composite layout key."""

    __tablename__ = "dashboard_layouts"
    __table_args__ = (
        CheckConstraint("breakpoint IN ('desktop', 'mobile')", name="ck_dashboard_layouts_breakpoint"),
        CheckConstraint("columns BETWEEN 1 AND 20", name="ck_dashboard_layouts_columns"),
    )

    dashboard_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("dashboards.id", ondelete="CASCADE"), primary_key=True
    )
    breakpoint: Mapped[str] = mapped_column(String(8), primary_key=True)
    columns: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")


class GadgetPlacement(Base):
    """Persist a validated integer rectangle linked to an instance and matching dashboard layout."""

    __tablename__ = "gadget_placements"
    __table_args__ = (
        CheckConstraint("x BETWEEN 0 AND 19", name="ck_gadget_placements_x"),
        CheckConstraint("y BETWEEN 0 AND 100000", name="ck_gadget_placements_y"),
        CheckConstraint("y + h <= 100000", name="ck_gadget_placements_bottom_bound"),
        CheckConstraint("w BETWEEN 1 AND 20", name="ck_gadget_placements_w"),
        CheckConstraint("h BETWEEN 1 AND 100000", name="ck_gadget_placements_h"),
        ForeignKeyConstraint(
            ["dashboard_id", "breakpoint"],
            ["dashboard_layouts.dashboard_id", "dashboard_layouts.breakpoint"],
            ondelete="CASCADE",
            name="fk_gadget_placements_layout",
        ),
        ForeignKeyConstraint(
            ["instance_id", "dashboard_id"],
            ["gadget_instances.id", "gadget_instances.dashboard_id"],
            ondelete="CASCADE",
            name="fk_gadget_placements_instance_dashboard",
        ),
    )

    dashboard_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    breakpoint: Mapped[str] = mapped_column(String(8), primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    w: Mapped[int] = mapped_column(Integer, nullable=False)
    h: Mapped[int] = mapped_column(Integer, nullable=False)


class DailyBrief(Base):
    """One immutable saved brief revision for a local date; regeneration appends, never overwrites.

    ``status`` flips to ``stale`` when captured document evidence disappears. The evidence
    cleanup physically scrubs aggregate prose and citation labels when any captured dependency
    is deleted, because those fields cannot be safely decomposed by support.
    """

    __tablename__ = "daily_briefs"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_daily_briefs_revision"),
        CheckConstraint("status IN ('current', 'stale')", name="ck_daily_briefs_status"),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="ck_daily_briefs_citations_array"),
        CheckConstraint(
            "(evidence_capture_version IS NULL AND evidence_capture_status IS NULL AND evidence_fact_count IS NULL) OR "
            "(evidence_capture_version = 1 AND evidence_capture_status IN ('captured','unavailable') "
            "AND evidence_fact_count BETWEEN 1 AND 40)",
            name="ck_daily_briefs_evidence_capture",
        ),
        UniqueConstraint("owner_id", "brief_date", "timezone", "revision", name="uq_daily_briefs_revision"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), nullable=False)
    brief_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="current")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    model_alias: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    evidence_capture_version: Mapped[int | None] = mapped_column(Integer)
    evidence_capture_status: Mapped[str | None] = mapped_column(String(16))
    evidence_fact_count: Mapped[int | None] = mapped_column(Integer)


class DailyBriefEvidence(Base):
    """Capture every prompted fact's exact document supports without canonical FKs.

    One row per support and one null-support marker for a truly independent fact
    preserve full prompt lineage after canonical Document cascades.
    """

    __tablename__ = "daily_brief_evidence"
    __table_args__ = (
        CheckConstraint("fact_ref BETWEEN 1 AND 40", name="ck_daily_brief_evidence_fact_ref"),
        CheckConstraint("support_index BETWEEN 0 AND 99", name="ck_daily_brief_evidence_support_index"),
        CheckConstraint("fact_kind IN ('tasks','goals','stories','events')", name="ck_daily_brief_evidence_fact_kind"),
        CheckConstraint("fact_hash ~ '^[0-9a-f]{64}$'", name="ck_daily_brief_evidence_fact_hash"),
        CheckConstraint(
            "(document_id IS NULL AND document_version_id IS NULL AND chunk_id IS NULL AND source_id IS NULL) OR "
            "(document_id IS NOT NULL AND document_version_id IS NOT NULL AND chunk_id IS NOT NULL AND source_id IS NOT NULL)",
            name="ck_daily_brief_evidence_document_tuple",
        ),
        UniqueConstraint("brief_id", "fact_ref", "support_index", name="uq_daily_brief_evidence_fact_support"),
        Index("ix_daily_brief_evidence_document_brief", "document_id", "brief_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    brief_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("daily_briefs.id", ondelete="CASCADE"), nullable=False,
    )
    fact_ref: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    fact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    support_index: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    document_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    chunk_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class BriefSchedule(Base):
    """Owner-editable daily brief schedule (default 07:00 Asia/Ho_Chi_Minh) read by the ARQ cron."""

    __tablename__ = "brief_schedules"
    __table_args__ = (
        CheckConstraint("hour BETWEEN 0 AND 23 AND minute BETWEEN 0 AND 59", name="ck_brief_schedules_time"),
        CheckConstraint(
            "schedule_owner IN ('internal_brief','automation') AND "
            "((schedule_owner = 'automation') = (automation_id IS NOT NULL))",
            name="ck_brief_schedules_owner",
        ),
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("owner.id", ondelete="CASCADE"), primary_key=True)
    # P10 single-owner invariant for logical job "daily_brief": exactly one of the internal cron
    # (schedule_owner="internal_brief", automation_id NULL) or one automation owns the slot.
    schedule_owner: Mapped[str] = mapped_column(String(16), nullable=False, server_default="internal_brief")
    automation_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    hour: Mapped[int] = mapped_column(Integer, nullable=False, server_default="7")
    minute: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Asia/Ho_Chi_Minh")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
