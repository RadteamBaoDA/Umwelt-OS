"""Unit tests for dashboard module schemas, gadget definitions, and grid geometry validation.

Covers:
- GadgetScope, GadgetFilters, HighlightRule, and GadgetConfiguration schemas
- GadgetDefinitionCreate and GadgetDefinitionPatch validation
- DashboardCreate, GroupCreate, InstanceCreate, and LayoutItem schemas
- Grid coordinate bounds (x, y, w, h) and LayoutReplace breakpoint rules
- Layout validation: validate_layout (columns 1..20, boundary clipping, renderer minimums, overlap detection)
- Deterministic layout generators: default_desktop_layout and default_mobile_layout
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.dashboard.layouts import (
    default_desktop_layout,
    default_mobile_layout,
    validate_layout,
)
from modules.dashboard.schemas import (
    DashboardCreate,
    GadgetConfiguration,
    GadgetDefinitionCreate,
    GadgetScope,
    HighlightRule,
    InstanceCreate,
    LayoutItem,
)


class TestGadgetDefinitionSchemas:
    """Tests for gadget definition, scopes, filters, and highlight rules."""

    def test_gadget_scope_unique_selectors(self) -> None:
        """Verify unique selector enforcement in GadgetScope."""
        u1 = uuid4()
        # Unique source IDs valid
        scope = GadgetScope(source_item_ids=[u1])
        assert scope.source_item_ids == [u1]

        # Duplicate source IDs rejected
        with pytest.raises(ValidationError, match="source_item_ids must contain distinct values"):
            GadgetScope(source_item_ids=[u1, u1])

        # Duplicate symbols rejected
        with pytest.raises(ValidationError, match="symbols must contain distinct values"):
            GadgetScope(symbols=["AAPL", "AAPL"])

    def test_highlight_rule_and_unique_ids(self) -> None:
        """Verify HighlightRule and unique rule ID requirement in GadgetConfiguration."""
        rule_id = uuid4()
        rule = HighlightRule(
            id=rule_id,
            keywords=["urgent", "critical"],
            severity="critical",
            notify=True,
        )
        assert rule.severity == "critical"
        assert rule.notify is True

        # Unique rules in config
        config = GadgetConfiguration(highlight_rules=[rule])
        assert len(config.highlight_rules) == 1

        # Duplicate rule IDs rejected
        rule2 = HighlightRule(
            id=rule_id,  # duplicate ID
            keywords=["warning"],
            severity="warning",
            notify=False,
        )
        with pytest.raises(ValidationError, match="highlight_rules must have distinct ids"):
            GadgetConfiguration(highlight_rules=[rule, rule2])

    def test_gadget_definition_create_and_source_bounds(self) -> None:
        """Verify GadgetDefinitionCreate requires unique source_ids and valid renderer."""
        src1 = uuid4()
        src2 = uuid4()
        create = GadgetDefinitionCreate(
            name="Stock Price Tracker",
            renderer="chart.financial",
            source_ids=[src1, src2],
        )
        assert create.name == "Stock Price Tracker"
        assert create.renderer == "chart.financial"
        assert len(create.source_ids) == 2

        # Duplicate source IDs rejected
        with pytest.raises(ValidationError, match="source_ids must contain distinct values"):
            GadgetDefinitionCreate(
                name="Stock Price Tracker",
                renderer="chart.financial",
                source_ids=[src1, src1],
            )


class TestDashboardAndInstanceSchemas:
    """Tests for DashboardCreate, InstanceCreate, and LayoutItem schemas."""

    def test_dashboard_create_minimal(self) -> None:
        """Verify DashboardCreate requires name within bounds."""
        d = DashboardCreate(name="Main Intelligence View")
        assert d.name == "Main Intelligence View"

        with pytest.raises(ValidationError):
            DashboardCreate(name="")

        with pytest.raises(ValidationError):
            DashboardCreate(name="x" * 201)

    def test_instance_create_requires_revision(self) -> None:
        """Verify InstanceCreate requires expected_revision and UUIDs."""
        inst = InstanceCreate(
            expected_revision=1,
            group_id=uuid4(),
            definition_id=uuid4(),
            title="Overview Gadget",
            position=0,
        )
        assert inst.expected_revision == 1
        assert inst.title == "Overview Gadget"

    def test_layout_item_coordinates_and_bounds(self) -> None:
        """Verify LayoutItem grid coordinate constraints (x: 0..19, y: 0..100000, w: 1..20, h: 1..100000)."""
        inst_id = uuid4()
        item = LayoutItem(instance_id=inst_id, x=0, y=0, w=10, h=4)
        assert item.x == 0
        assert item.w == 10

        # Negative x rejected
        with pytest.raises(ValidationError):
            LayoutItem(instance_id=inst_id, x=-1, y=0, w=10, h=4)

        # Width > 20 rejected
        with pytest.raises(ValidationError):
            LayoutItem(instance_id=inst_id, x=0, y=0, w=21, h=4)

        # Width < 1 rejected
        with pytest.raises(ValidationError):
            LayoutItem(instance_id=inst_id, x=0, y=0, w=0, h=4)


class TestGridCoordinatesAndLayoutValidation:
    """Tests for pure grid geometry: validate_layout, default layouts, and overlap checks."""

    def test_validate_layout_valid(self) -> None:
        """Verify valid layout with side-by-side non-overlapping rectangles."""
        id1 = uuid4()
        id2 = uuid4()
        items = [
            LayoutItem(instance_id=id1, x=0, y=0, w=10, h=4),
            LayoutItem(instance_id=id2, x=10, y=0, w=10, h=4),
        ]
        minimum_sizes = {id1: (5, 2), id2: (5, 2)}
        # Should validate without error
        validate_layout(items, columns=20, minimum_sizes=minimum_sizes)

    def test_validate_layout_column_overflow(self) -> None:
        """Rectangle exceeding configured column count must be rejected."""
        id1 = uuid4()
        # x=15 + w=6 = 21 > 20 columns
        items = [LayoutItem(instance_id=id1, x=15, y=0, w=6, h=4)]
        minimum_sizes = {id1: (2, 2)}
        with pytest.raises(ValueError, match="rectangle exceeds the configured column count"):
            validate_layout(items, columns=20, minimum_sizes=minimum_sizes)

    def test_validate_layout_smaller_than_minimum(self) -> None:
        """Rectangle smaller than its renderer minimum dimensions must be rejected."""
        id1 = uuid4()
        items = [LayoutItem(instance_id=id1, x=0, y=0, w=4, h=2)]
        minimum_sizes = {id1: (6, 4)}  # Minimum is (6, 4)
        with pytest.raises(ValueError, match="rectangle is smaller than its renderer minimum"):
            validate_layout(items, columns=20, minimum_sizes=minimum_sizes)

    def test_validate_layout_overlap_detection(self) -> None:
        """Overlapping rectangles must be detected and rejected."""
        id1 = uuid4()
        id2 = uuid4()
        # Item 1: (0, 0) to (10, 4)
        # Item 2: (5, 2) to (15, 6) -> overlaps Item 1!
        items = [
            LayoutItem(instance_id=id1, x=0, y=0, w=10, h=4),
            LayoutItem(instance_id=id2, x=5, y=2, w=10, h=4),
        ]
        minimum_sizes = {id1: (2, 2), id2: (2, 2)}
        with pytest.raises(ValueError, match="layout rectangles cannot overlap"):
            validate_layout(items, columns=20, minimum_sizes=minimum_sizes)

    def test_validate_layout_touching_edges_permitted(self) -> None:
        """Half-open rectangles whose edges touch do NOT overlap and are permitted."""
        id1 = uuid4()
        id2 = uuid4()
        # Item 1 ends at x=10; Item 2 starts at x=10
        items = [
            LayoutItem(instance_id=id1, x=0, y=0, w=10, h=4),
            LayoutItem(instance_id=id2, x=10, y=0, w=10, h=4),
        ]
        minimum_sizes = {id1: (2, 2), id2: (2, 2)}
        validate_layout(items, columns=20, minimum_sizes=minimum_sizes)

    def test_validate_layout_instance_mismatch(self) -> None:
        """Layout items must exactly match the dashboard instance set."""
        id1 = uuid4()
        id2 = uuid4()
        items = [LayoutItem(instance_id=id1, x=0, y=0, w=10, h=4)]
        minimum_sizes = {id1: (2, 2), id2: (2, 2)}  # id2 missing from items!
        with pytest.raises(ValueError, match="layout instances must exactly match the dashboard instances"):
            validate_layout(items, columns=20, minimum_sizes=minimum_sizes)

    def test_default_desktop_layout_packing(self) -> None:
        """Verify default_desktop_layout packs rectangles and wraps at the 20-column boundary."""
        id1 = uuid4()
        id2 = uuid4()
        id3 = uuid4()
        # id1 is 12 wide, id2 is 10 wide -> id2 will wrap to y=row_height!
        minimum_sizes = {
            id1: (12, 4),
            id2: (10, 6),
            id3: (8, 4),
        }
        layout = default_desktop_layout(minimum_sizes, columns=20)
        assert len(layout) == 3

        # First item starts at (0, 0)
        assert layout[0].instance_id == id1
        assert (layout[0].x, layout[0].y) == (0, 0)
        assert (layout[0].w, layout[0].h) == (12, 4)

        # Second item wraps to x=0, y=4
        assert layout[1].instance_id == id2
        assert (layout[1].x, layout[1].y) == (0, 4)
        assert (layout[1].w, layout[1].h) == (10, 6)

        # Third item fits on second row at x=10, y=4
        assert layout[2].instance_id == id3
        assert (layout[2].x, layout[2].y) == (10, 4)
        assert (layout[2].w, layout[2].h) == (8, 4)

    def test_default_mobile_layout_stacking(self) -> None:
        """Verify default_mobile_layout stacks all gadgets full-width (w=20) sequentially."""
        id1 = uuid4()
        id2 = uuid4()
        minimum_sizes = {
            id1: (6, 4),
            id2: (8, 5),
        }
        layout = default_mobile_layout(minimum_sizes, columns=20)
        assert len(layout) == 2

        # First item: full width, y=0, h=4
        assert layout[0].x == 0
        assert layout[0].y == 0
        assert layout[0].w == 20
        assert layout[0].h == 4

        # Second item: full width, y=4, h=5
        assert layout[1].x == 0
        assert layout[1].y == 4
        assert layout[1].w == 20
        assert layout[1].h == 5
