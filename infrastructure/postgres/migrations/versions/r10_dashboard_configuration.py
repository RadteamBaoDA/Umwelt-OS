"""Persist owner-selected dashboards, reusable gadget definitions, and layouts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r10_dashboard_configuration"
# Re-parented at the develop merges (now after p07_specialist_browser_reads); r10 was never shipped.
down_revision: str | Sequence[str] | None = "p07_specialist_browser_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the six configuration tables with ownership and same-dashboard relational constraints."""
    op.create_table(
        "dashboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_dashboards_revision"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "dashboard_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dashboard_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_dashboard_groups_position"),
        # Composite uniqueness supports the instance FK that prevents cross-dashboard grouping.
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "dashboard_id", name="uq_dashboard_groups_id_dashboard"),
    )
    op.create_table(
        "gadget_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("renderer", sa.String(length=64), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("highlight_rules", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_gadget_definitions_revision"),
        sa.CheckConstraint("jsonb_typeof(source_ids) = 'array'", name="ck_gadget_definitions_source_ids_array"),
        sa.ForeignKeyConstraint(["owner_id"], ["owner.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "gadget_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dashboard_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_gadget_instances_position"),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"),
        # Group deletion cascades its instances; referenced definitions remain protected by RESTRICT.
        sa.ForeignKeyConstraint(
            ["group_id", "dashboard_id"],
            ["dashboard_groups.id", "dashboard_groups.dashboard_id"],
            ondelete="CASCADE",
            name="fk_gadget_instances_group_dashboard",
        ),
        sa.ForeignKeyConstraint(
            ["definition_id"], ["gadget_definitions.id"], ondelete="RESTRICT",
            name="fk_gadget_instances_definition",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "dashboard_id", name="uq_gadget_instances_id_dashboard"),
    )
    op.create_table(
        "dashboard_layouts",
        sa.Column("dashboard_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("breakpoint", sa.String(length=8), nullable=False),
        sa.Column("columns", sa.Integer(), server_default="20", nullable=False),
        sa.CheckConstraint("breakpoint IN ('desktop', 'mobile')", name="ck_dashboard_layouts_breakpoint"),
        sa.CheckConstraint("columns BETWEEN 1 AND 20", name="ck_dashboard_layouts_columns"),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("dashboard_id", "breakpoint"),
    )
    op.create_table(
        "gadget_placements",
        sa.Column("dashboard_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("breakpoint", sa.String(length=8), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("x", sa.Integer(), nullable=False),
        sa.Column("y", sa.Integer(), nullable=False),
        sa.Column("w", sa.Integer(), nullable=False),
        sa.Column("h", sa.Integer(), nullable=False),
        sa.CheckConstraint("x BETWEEN 0 AND 19", name="ck_gadget_placements_x"),
        sa.CheckConstraint("y BETWEEN 0 AND 100000", name="ck_gadget_placements_y"),
        sa.CheckConstraint("y + h <= 100000", name="ck_gadget_placements_bottom_bound"),
        sa.CheckConstraint("w BETWEEN 1 AND 20", name="ck_gadget_placements_w"),
        sa.CheckConstraint("h BETWEEN 1 AND 100000", name="ck_gadget_placements_h"),
        # Composite FKs keep placements within their dashboard and breakpoint, cascading on deletion.
        sa.ForeignKeyConstraint(
            ["dashboard_id", "breakpoint"],
            ["dashboard_layouts.dashboard_id", "dashboard_layouts.breakpoint"],
            ondelete="CASCADE",
            name="fk_gadget_placements_layout",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id", "dashboard_id"],
            ["gadget_instances.id", "gadget_instances.dashboard_id"],
            ondelete="CASCADE",
            name="fk_gadget_placements_instance_dashboard",
        ),
        sa.PrimaryKeyConstraint("dashboard_id", "breakpoint", "instance_id"),
    )


def downgrade() -> None:
    """Drop placements before layouts and instances before their referenced groups and definitions."""
    # Reverse dependency order avoids leaving dependent configuration behind.
    op.drop_table("gadget_placements")
    op.drop_table("dashboard_layouts")
    op.drop_table("gadget_instances")
    op.drop_table("gadget_definitions")
    op.drop_table("dashboard_groups")
    op.drop_table("dashboards")
