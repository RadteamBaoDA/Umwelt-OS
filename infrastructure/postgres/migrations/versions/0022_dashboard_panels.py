"""Give each dashboard a permanent Overview panel and independent panel coordinates.

Existing gadgets retain their group, width and height. The first group by the old
ordering becomes Overview; its saved placements and every other group's placements
are rebased to each group's own vertical origin.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_dashboard_panels"
down_revision: str | Sequence[str] | None = "0021_harden_umwelt_app_group_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STACK_OFFSETS = """
WITH extents AS (
    SELECT grp.dashboard_id, grp.id AS group_id, grp.position, grp.is_overview,
           layout.breakpoint, COALESCE(MAX(place.y + place.h), 0) AS height
    FROM dashboard_groups AS grp
    JOIN dashboard_layouts AS layout ON layout.dashboard_id = grp.dashboard_id
    LEFT JOIN gadget_instances AS inst
      ON inst.dashboard_id = grp.dashboard_id AND inst.group_id = grp.id
    LEFT JOIN gadget_placements AS place
      ON place.dashboard_id = grp.dashboard_id AND place.instance_id = inst.id
     AND place.breakpoint = layout.breakpoint
    GROUP BY grp.dashboard_id, grp.id, grp.position, grp.is_overview, layout.breakpoint
), offsets AS (
    SELECT extents.*,
           COALESCE(SUM(height) OVER (
               PARTITION BY dashboard_id, breakpoint
               ORDER BY is_overview DESC, position, group_id
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
           ), 0) AS y_offset
    FROM extents
)
"""


def upgrade() -> None:
    """Mark one Overview per dashboard and rebase each group's layout without discarding gadgets."""
    op.add_column(
        "dashboard_groups",
        sa.Column("is_overview", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The index makes concurrent or manual writes unable to create two Overview groups.
    op.create_index(
        "uq_dashboard_groups_one_overview", "dashboard_groups", ["dashboard_id"],
        unique=True, postgresql_where=sa.text("is_overview"),
    )
    op.execute("""
        WITH first_group AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY dashboard_id ORDER BY position, id
            ) AS ordinal
            FROM dashboard_groups
        )
        UPDATE dashboard_groups AS grp SET is_overview = TRUE
        FROM first_group WHERE grp.id = first_group.id AND first_group.ordinal = 1
    """)
    op.execute("""
        INSERT INTO dashboard_groups (id, dashboard_id, name, position, is_overview)
        SELECT uuid_generate_v7(), dash.id, 'Overview', 0, TRUE
        FROM dashboards AS dash
        WHERE NOT EXISTS (
            SELECT 1 FROM dashboard_groups AS grp WHERE grp.dashboard_id = dash.id
        )
    """)
    op.execute("""
        WITH origins AS (
            SELECT inst.dashboard_id, inst.group_id, place.breakpoint,
                   MIN(place.y) AS first_y
            FROM gadget_placements AS place
            JOIN gadget_instances AS inst
              ON inst.id = place.instance_id AND inst.dashboard_id = place.dashboard_id
            GROUP BY inst.dashboard_id, inst.group_id, place.breakpoint
        )
        UPDATE gadget_placements AS place SET y = place.y - origins.first_y
        FROM gadget_instances AS inst
        JOIN origins ON origins.dashboard_id = inst.dashboard_id
                    AND origins.group_id = inst.group_id
        WHERE place.instance_id = inst.id AND place.dashboard_id = inst.dashboard_id
          AND place.breakpoint = origins.breakpoint
    """)


def downgrade() -> None:
    """Stack panels back into collision-free dashboard rows, failing before an unsafe overflow."""
    bind = op.get_bind()
    if bind.execute(sa.text(
        _STACK_OFFSETS + "SELECT 1 FROM offsets WHERE y_offset + height > 100000 LIMIT 1"  # noqa: S608 - constant SQL
    )).first() is not None:
        raise RuntimeError("Dashboard panel heights exceed the old layout row limit")
    op.execute(_STACK_OFFSETS + """
        UPDATE gadget_placements AS place SET y = place.y + offsets.y_offset
        FROM gadget_instances AS inst
        JOIN offsets ON offsets.dashboard_id = inst.dashboard_id
                    AND offsets.group_id = inst.group_id
        WHERE place.instance_id = inst.id AND place.dashboard_id = inst.dashboard_id
          AND place.breakpoint = offsets.breakpoint
    """)  # noqa: S608 - constant SQL, no input
    op.drop_index("uq_dashboard_groups_one_overview", table_name="dashboard_groups")
    op.drop_column("dashboard_groups", "is_overview")
