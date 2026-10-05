"""Pure geometry validation and deterministic default dashboard layouts."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from modules.dashboard.schemas import LayoutItem

MAX_LAYOUT_ROWS = 100_000
MAX_LAYOUT_ITEMS = 100
MAX_COLUMNS = 20


def validate_layout(
    items: Sequence[LayoutItem],
    columns: int,
    minimum_sizes: Mapping[UUID, tuple[int, int]],
) -> None:
    """Reject invalid, undersized, overlapping, or non-owned rectangles without rewriting them.

    `minimum_sizes` must contain exactly the current dashboard instance IDs; callers separately
    compare that membership to the persisted instance set before invoking this pure validator.
    """
    if type(columns) is not int or not 1 <= columns <= MAX_COLUMNS:
        raise ValueError("columns must be an integer from 1 through 20")
    if len(items) > MAX_LAYOUT_ITEMS:
        raise ValueError("a dashboard cannot contain more than 100 placements")
    identifiers = [item.instance_id for item in items]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("each instance may appear only once")
    if set(identifiers) != set(minimum_sizes):
        raise ValueError("layout instances must exactly match the dashboard instances")

    for item in items:
        if item.x + item.w > columns:
            raise ValueError("rectangle exceeds the configured column count")
        if item.y + item.h > MAX_LAYOUT_ROWS:
            raise ValueError("rectangle exceeds the maximum configured row")
        minimum_width, minimum_height = minimum_sizes[item.instance_id]
        if item.w < minimum_width or item.h < minimum_height:
            raise ValueError("rectangle is smaller than its renderer minimum")

    # Half-open rectangle intersection permits edges to touch; the 100-item bound keeps this O(n²)
    # check simple and predictable. ponytail: pairwise scan, spatial index only if the 100-item ceiling changes.
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            overlaps = (
                left.x < right.x + right.w
                and right.x < left.x + left.w
                and left.y < right.y + right.h
                and right.y < left.y + left.h
            )
            if overlaps:
                raise ValueError("layout rectangles cannot overlap")


def default_desktop_layout(
    minimum_sizes: Mapping[UUID, tuple[int, int]], columns: int = MAX_COLUMNS
) -> list[LayoutItem]:
    """Pack renderer-minimum rectangles in stable input order, wrapping at the column edge."""
    if type(columns) is not int or not 1 <= columns <= MAX_COLUMNS:
        raise ValueError("columns must be an integer from 1 through 20")
    if len(minimum_sizes) > MAX_LAYOUT_ITEMS:
        raise ValueError("a dashboard cannot contain more than 100 placements")

    placements: list[LayoutItem] = []
    x = 0
    y = 0
    row_height = 0
    for instance_id, (width, height) in minimum_sizes.items():
        if width < 1 or height < 1 or width > columns:
            raise ValueError("renderer minimum cannot fit configured desktop columns")
        if x + width > columns:
            y += row_height
            x = 0
            row_height = 0
        if y + height > MAX_LAYOUT_ROWS:
            raise ValueError("default layout exceeds the maximum configured row")
        placements.append(
            LayoutItem(instance_id=instance_id, x=x, y=y, w=width, h=height)
        )
        x += width
        row_height = max(row_height, height)
    validate_layout(placements, columns, minimum_sizes)
    return placements


def default_mobile_layout(
    minimum_sizes: Mapping[UUID, tuple[int, int]], columns: int = MAX_COLUMNS
) -> list[LayoutItem]:
    """Stack each gadget full-width in stable input order with its renderer minimum height."""
    if type(columns) is not int or not 1 <= columns <= MAX_COLUMNS:
        raise ValueError("columns must be an integer from 1 through 20")
    if len(minimum_sizes) > MAX_LAYOUT_ITEMS:
        raise ValueError("a dashboard cannot contain more than 100 placements")

    placements: list[LayoutItem] = []
    y = 0
    for instance_id, (minimum_width, height) in minimum_sizes.items():
        if minimum_width > columns:
            raise ValueError("renderer minimum cannot fit configured mobile columns")
        if y + height > MAX_LAYOUT_ROWS:
            raise ValueError("default layout exceeds the maximum configured row")
        placements.append(LayoutItem(instance_id=instance_id, x=0, y=y, w=columns, h=height))
        y += height
    validate_layout(placements, columns, minimum_sizes)
    return placements
