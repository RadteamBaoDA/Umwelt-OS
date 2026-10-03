"""Create canonical timeline event, evidence, correction, suppression, and work tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "p05_timeline_events"
down_revision: str | Sequence[str] | None = "p04_entity_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create canonical events and their bounded provenance and extraction records."""
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()
    op.create_table(
        "timeline_events",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("source_id", uuid, sa.ForeignKey("sources.id", ondelete="SET NULL")),
        sa.Column("type", sa.String(64), nullable=False), sa.Column("subtype", sa.String(64)),
        sa.Column("title", sa.String(300), nullable=False), sa.Column("summary", sa.Text()),
        sa.Column("importance_score", sa.Float()), sa.Column("confidence", sa.Float()),
        sa.Column("metadata", jsonb, server_default="{}", nullable=False),
        sa.Column("origin", sa.String(16), nullable=False), sa.Column("date_precision", sa.String(16), nullable=False),
        sa.Column("extraction_identity", sa.String(256)), sa.Column("candidate_hash", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("occurred_date", sa.Date()), sa.Column("end_date", sa.Date()),
        sa.Column("occurrence_timezone", sa.String(64)), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)), sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("owner_fields", jsonb, server_default="[]", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_timeline_events_revision"),
        sa.CheckConstraint("origin IN ('manual', 'derived')", name="ck_timeline_events_origin"),
        sa.CheckConstraint("date_precision IN ('timed', 'date', 'unknown')", name="ck_timeline_events_precision"),
        sa.CheckConstraint("importance_score IS NULL OR (importance_score >= 0 AND importance_score <= 1)", name="ck_timeline_events_importance"),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_timeline_events_confidence"),
        sa.CheckConstraint("(date_precision = 'timed' AND started_at IS NOT NULL AND occurred_date IS NULL AND end_date IS NULL) OR (date_precision = 'date' AND started_at IS NULL AND occurred_date IS NOT NULL) OR (date_precision = 'unknown' AND started_at IS NULL AND occurred_date IS NULL AND end_date IS NULL)", name="ck_timeline_events_time_shape"),
        sa.CheckConstraint("ended_at IS NULL OR (started_at IS NOT NULL AND ended_at >= started_at)", name="ck_timeline_events_timed_range"),
        sa.CheckConstraint("end_date IS NULL OR (occurred_date IS NOT NULL AND end_date >= occurred_date)", name="ck_timeline_events_date_range"),
        sa.CheckConstraint("valid_to IS NULL OR (valid_from IS NOT NULL AND valid_to > valid_from)", name="ck_timeline_events_validity"),
        sa.UniqueConstraint("extraction_identity", "candidate_hash", name="uq_timeline_event_candidate_identity"),
    )
    for name, columns in (
        ("ix_timeline_events_timed", ["started_at", "id"]),
        ("ix_timeline_events_date", ["occurred_date", "id"]),
        ("ix_timeline_events_unknown", ["created_at", "id"]),
        ("ix_timeline_events_source", ["source_id", "created_at"]),
        ("ix_timeline_events_type", ["type", "subtype"]),
    ):
        op.create_index(name, "timeline_events", columns)
    op.create_table(
        "timeline_event_participants",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("event_id", uuid, sa.ForeignKey("timeline_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", uuid, sa.ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("role", sa.String(64), nullable=False), sa.Column("metadata", jsonb, server_default="{}", nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.UniqueConstraint("event_id", "entity_id", "role", name="uq_timeline_event_participant_role"),
        sa.CheckConstraint("origin IN ('manual', 'derived')", name="ck_timeline_participant_origin"),
    )
    op.create_index("ix_timeline_participant_entity", "timeline_event_participants", ["entity_id", "event_id"])
    op.create_table(
        "timeline_event_evidence",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("event_id", uuid, sa.ForeignKey("timeline_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", uuid, sa.ForeignKey("sources.id", ondelete="SET NULL")),
        sa.Column("document_id", uuid, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("document_version_id", uuid, sa.ForeignKey("document_versions.id", ondelete="SET NULL")),
        sa.Column("chunk_id", uuid, sa.ForeignKey("document_chunks.id", ondelete="SET NULL")),
        sa.Column("version_number", sa.Integer()), sa.Column("source_generation", sa.Integer()),
        sa.Column("extraction_identity", sa.String(256)), sa.Column("candidate_hash", sa.String(64)),
        sa.Column("confidence", sa.Float()), sa.Column("extracted_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True)), sa.Column("title_snapshot", sa.String(500)),
        sa.Column("url_snapshot", sa.Text()), sa.Column("evidence_metadata", jsonb, server_default="{}", nullable=False),
        sa.Column("excerpt", sa.Text()), sa.Column("metadata_is_version_snapshot", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("event_id", "document_version_id", "chunk_id", name="uq_timeline_event_evidence"),
    )
    op.create_index("ix_timeline_event_evidence_source", "timeline_event_evidence", ["source_id", "document_id"])
    op.create_index("ix_timeline_event_evidence_version", "timeline_event_evidence", ["document_version_id", "chunk_id"])
    op.create_table(
        "timeline_participant_evidence", sa.Column("id", uuid, primary_key=True),
        sa.Column("participant_id", uuid, sa.ForeignKey("timeline_event_participants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_evidence_id", uuid, sa.ForeignKey("timeline_event_evidence.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("participant_id", "event_evidence_id", name="uq_timeline_participant_evidence"),
    )
    op.create_table(
        "timeline_event_audits", sa.Column("id", uuid, primary_key=True),
        sa.Column("event_id", uuid, sa.ForeignKey("timeline_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False), sa.Column("prior_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False), sa.Column("changed_json", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_timeline_event_audits_event", "timeline_event_audits", ["event_id", "created_at"])
    op.create_table(
        "timeline_event_suppressions", sa.Column("id", uuid, primary_key=True),
        # Suppressions deliberately keep detached identity after source deletion.
        sa.Column("document_id", uuid),
        sa.Column("document_version_id", uuid),
        sa.Column("source_id", uuid),
        sa.Column("source_generation", sa.Integer()), sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_version_id", "candidate_hash", name="uq_timeline_event_suppression_identity"),
    )
    op.create_table(
        "timeline_extraction_work", sa.Column("id", uuid, primary_key=True),
        sa.Column("document_id", uuid, sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_version_id", uuid, sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", uuid, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False), sa.Column("extractor_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False), sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_owner", sa.String(64)), sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)), sa.Column("dependency_fingerprint", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_version_id", "source_generation", "extractor_version", "prompt_version", name="uq_timeline_extraction_work_identity"),
        sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'blocked', 'failed')", name="ck_timeline_extraction_work_status"),
        sa.CheckConstraint("source_generation >= 1 AND attempt >= 0", name="ck_timeline_extraction_work_bounds"),
    )
    op.create_index("ix_timeline_extraction_recovery", "timeline_extraction_work", ["status", "next_attempt_at", "lease_expires_at"])
    op.create_table(
        "timeline_extraction_results", sa.Column("id", uuid, primary_key=True),
        sa.Column("work_id", uuid, sa.ForeignKey("timeline_extraction_work.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(200)), sa.Column("proposals_json", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("work_id", name="uq_timeline_extraction_result_work"),
    )


def downgrade() -> None:
    """Drop timeline extraction, correction, evidence, and canonical event tables."""
    for table in ("timeline_extraction_results", "timeline_extraction_work", "timeline_event_suppressions",
                  "timeline_event_audits", "timeline_participant_evidence", "timeline_event_evidence",
                  "timeline_event_participants"):
        op.drop_table(table)
    for name in ("ix_timeline_events_type", "ix_timeline_events_source", "ix_timeline_events_unknown",
                 "ix_timeline_events_date", "ix_timeline_events_timed"):
        op.drop_index(name, table_name="timeline_events")
    op.drop_table("timeline_events")
