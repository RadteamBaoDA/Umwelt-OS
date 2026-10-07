"""Strict, bounded request and configuration schemas for saved dashboards."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

MAX_DASHBOARDS_PER_OWNER = 50
MAX_GROUPS_PER_DASHBOARD = 50
MAX_DEFINITIONS_PER_OWNER = 200
MAX_INSTANCES_PER_DASHBOARD = 100
MAX_SOURCES_PER_DEFINITION = 32
MAX_RULES_PER_DEFINITION = 32
MAX_TOPICS_PER_RULE = 8
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

Revision = Annotated[StrictInt, Field(ge=1, le=9_007_199_254_740_991)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

class StrictConfiguration(BaseModel):
    """Reject undeclared fields while individual numeric and boolean fields enforce strict types."""

    model_config = ConfigDict(extra="forbid")


class GadgetScope(StrictConfiguration):
    """Store only bounded selectors; authorization is checked by the data owner at read time."""

    source_item_ids: list[UUID] = Field(default_factory=list, max_length=100)
    channel_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=32
    )
    symbols: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=32
    )
    regions: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=32
    )
    map_layer_ids: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=32
    )
    cii_country_codes: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = Field(
        default_factory=list, max_length=31
    )
    metrics: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=32)
    lookback_days: StrictInt | None = Field(default=None, ge=1, le=366)

    @model_validator(mode="after")
    def ensure_unique_selectors(self) -> "GadgetScope":
        """Reject duplicates so selectors have stable canonical fingerprints and storage."""
        for name, values in self.model_dump().items():
            if isinstance(values, list) and len(values) != len(set(values)):
                raise ValueError(f"{name} must contain distinct values")
        return self


class GadgetFilters(StrictConfiguration):
    """Persist bounded text filters and map display preferences, never executable queries.

    ``map_engine`` and ``show_precise_locations`` are renderer preferences only;
    map data authorization and source/date/region bounds are validated separately.
    """

    keywords: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=32
    )
    exclude_keywords: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=32
    )
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 25
    map_engine: Literal["globe", "flat"] | None = None
    show_precise_locations: StrictBool = False


class HighlightRule(StrictConfiguration):
    """Represent a non-executable highlight rule; notification behavior belongs to R12."""

    id: UUID
    keywords: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=16
    )
    severity: Literal["info", "warning", "critical"]
    notify: StrictBool
    topic_ids: list[UUID] = Field(default_factory=list, max_length=MAX_TOPICS_PER_RULE)
    source_ids: list[UUID] = Field(default_factory=list, max_length=MAX_SOURCES_PER_DEFINITION)
    exclude_source_ids: list[UUID] = Field(default_factory=list, max_length=MAX_SOURCES_PER_DEFINITION)

    @model_validator(mode="after")
    def ensure_conditions(self) -> "HighlightRule":
        """Require a keyword or topic condition and keep every ID list distinct and non-overlapping."""
        if not self.keywords and not self.topic_ids:
            raise ValueError("a rule needs at least one keyword or topic")
        for name in ("topic_ids", "source_ids", "exclude_source_ids"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain distinct values")
        if set(self.source_ids) & set(self.exclude_source_ids):
            raise ValueError("source_ids and exclude_source_ids must not overlap")
        return self


class GadgetConfiguration(StrictConfiguration):
    """Validate the v1 scope, filters, and rule structures stored with a definition."""

    scope: GadgetScope = Field(default_factory=GadgetScope)
    filters: GadgetFilters = Field(default_factory=GadgetFilters)
    highlight_rules: list[HighlightRule] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def ensure_unique_rule_ids(self) -> "GadgetConfiguration":
        """Keep rule identity deterministic so edits and fingerprints cannot alias rules."""
        identifiers = [rule.id for rule in self.highlight_rules]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("highlight_rules must have distinct ids")
        return self


class GadgetDefinitionCreate(StrictConfiguration):
    """Validate reusable definition creation without accepting owner or runtime payload data."""

    name: Name
    renderer: str = Field(min_length=1, max_length=64)
    source_ids: list[UUID] = Field(default_factory=list, max_length=MAX_SOURCES_PER_DEFINITION)
    scope: GadgetScope = Field(default_factory=GadgetScope)
    filters: GadgetFilters = Field(default_factory=GadgetFilters)
    highlight_rules: list[HighlightRule] = Field(
        default_factory=list, max_length=MAX_RULES_PER_DEFINITION
    )

    @model_validator(mode="after")
    def ensure_valid_source_ids(self) -> "GadgetDefinitionCreate":
        """Require unique sources and unique rule IDs before owner persistence."""
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("source_ids must contain distinct values")
        GadgetConfiguration(
            scope=self.scope, filters=self.filters, highlight_rules=self.highlight_rules
        )
        return self


class GadgetDefinitionPatch(StrictConfiguration):
    """Validate a partial definition update guarded by its independent revision."""

    expected_revision: Revision
    name: Name | None = None
    renderer: str | None = Field(default=None, min_length=1, max_length=64)
    source_ids: list[UUID] | None = Field(default=None, max_length=MAX_SOURCES_PER_DEFINITION)
    scope: GadgetScope | None = None
    filters: GadgetFilters | None = None
    highlight_rules: list[HighlightRule] | None = Field(
        default=None, max_length=MAX_RULES_PER_DEFINITION
    )

    @model_validator(mode="after")
    def ensure_unique_source_ids(self) -> "GadgetDefinitionPatch":
        """Reject duplicate source IDs and revalidate any supplied bounded configuration blocks."""
        if self.source_ids is not None and len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("source_ids must contain distinct values")
        GadgetConfiguration(
            scope=self.scope or GadgetScope(),
            filters=self.filters or GadgetFilters(),
            highlight_rules=self.highlight_rules or [],
        )
        return self


class DashboardCreate(StrictConfiguration):
    """Validate dashboard creation with no caller-controlled ownership or revision."""

    name: Name


class DashboardPatch(StrictConfiguration):
    """Validate a name edit against the shared dashboard revision."""

    expected_revision: Revision
    name: Name


class GroupCreate(StrictConfiguration):
    """Validate group creation against the owning dashboard revision and position bounds."""

    expected_revision: Revision
    name: Name
    position: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 0


class GroupPatch(StrictConfiguration):
    """Validate group changes against the owning dashboard revision."""

    expected_revision: Revision
    name: Name | None = None
    position: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None


class InstanceCreate(StrictConfiguration):
    """Validate a dashboard placement reference without accepting renderer payloads."""

    expected_revision: Revision
    group_id: UUID
    definition_id: UUID
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    position: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 0


class InstancePatch(StrictConfiguration):
    """Validate instance title or group movement under the shared dashboard revision."""

    expected_revision: Revision
    group_id: UUID | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    position: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] | None = None


class LayoutItem(StrictConfiguration):
    """Carry one integer rectangle keyed to an instance in the target dashboard."""

    instance_id: UUID
    x: Annotated[StrictInt, Field(ge=0, le=19)]
    y: Annotated[StrictInt, Field(ge=0, le=100_000)]
    w: Annotated[StrictInt, Field(ge=1, le=20)]
    h: Annotated[StrictInt, Field(ge=1, le=100_000)]


class LayoutReplace(StrictConfiguration):
    """Validate one breakpoint replacement; exact instance membership is checked by its owner."""

    expected_revision: Revision
    breakpoint: Literal["desktop", "mobile"]
    columns: Annotated[StrictInt, Field(ge=1, le=20)] = 20
    items: list[LayoutItem] = Field(max_length=100)


class PresetPreviewRequest(StrictConfiguration):
    """Describe an owner-selected source mapping and optional preview target."""

    slot_sources: dict[str, Annotated[list[UUID], Field(max_length=32)]] = Field(
        default_factory=dict, max_length=16
    )
    target_dashboard_id: UUID | None = None

    @model_validator(mode="after")
    def ensure_unique_slot_sources(self) -> "PresetPreviewRequest":
        """Reject duplicate source mappings so preset previews remain canonical and bounded."""
        for slot_id, source_ids in self.slot_sources.items():
            if len(source_ids) != len(set(source_ids)):
                raise ValueError(f"slot {slot_id} must contain distinct source IDs")
        return self


class PresetApplyRequest(PresetPreviewRequest):
    """Validate preset apply intent; replacement requires an explicit current revision."""

    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["create", "replace"] = "create"
    name: Name | None = None
    expected_revision: Revision | None = None
    replace_confirmed: StrictBool = False

    @model_validator(mode="after")
    def ensure_mode_fields_match(self) -> "PresetApplyRequest":
        """Prevent accidental replacement and reject replacement-only fields in create mode."""
        if self.mode == "create":
            if self.target_dashboard_id is not None or self.expected_revision is not None or self.replace_confirmed:
                raise ValueError("create mode cannot include replacement fields")
        elif self.target_dashboard_id is None or self.expected_revision is None or not self.replace_confirmed:
            raise ValueError("replace mode requires target, expected_revision, and replace_confirmed")
        return self


class DashboardWarning(StrictConfiguration):
    """Describe planned runtime or unavailable source metadata without carrying source content."""

    code: str
    capability: str | None = None
    source_id: UUID | None = None
    setup_group: str | None = None


class DashboardHighlightRead(StrictConfiguration):
    """Expose one explainable current-version match for a configured highlight rule."""
    document_id: UUID
    document_version_id: UUID
    source_id: UUID
    title: str
    observed_at: datetime
    rule_id: UUID
    matched_keywords: list[str] = Field(max_length=16)
    severity: Literal["info", "warning", "critical"]
    notify: StrictBool
    reason: str = Field(max_length=1000)


class DashboardGroupRead(StrictConfiguration):
    """Expose one dashboard-owned group and its ordering fields."""

    id: UUID
    name: str
    position: int
    dashboard_id: UUID


class GadgetDefinitionRead(StrictConfiguration):
    """Expose bounded renderer configuration and adapter metadata with its revision.

    ``available`` indicates a production data adapter is present, not runtime or provider acceptance.
    """

    id: UUID
    name: str
    revision: Revision
    renderer: str
    source_ids: list[UUID]
    scope: dict[str, object]
    filters: dict[str, object]
    highlight_rules: list[dict[str, object]]
    config_version: int = 1
    runtime_state: Literal["planned", "available"] = "planned"
    warnings: list[DashboardWarning] = Field(default_factory=list)


class GadgetInstanceRead(StrictConfiguration):
    """Describe one dashboard placement linked to reusable definition configuration."""

    id: UUID
    group_id: UUID
    definition_id: UUID
    title: str | None
    position: int
    definition: GadgetDefinitionRead


class DashboardSummary(StrictConfiguration):
    """Expose a dashboard name and revision for owner-library selection."""

    id: UUID
    name: str
    revision: Revision
    created_at: datetime
    updated_at: datetime


class LayoutItemRead(StrictConfiguration):
    """Return one saved integer placement rectangle for the selected breakpoint."""

    instance_id: UUID
    x: int
    y: int
    w: int
    h: int


class DashboardBreakpointRead(StrictConfiguration):
    """Expose independent columns and complete placements for one dashboard breakpoint."""

    columns: int
    items: list[LayoutItemRead]


class DashboardLayoutsRead(StrictConfiguration):
    """Return both desktop and mobile saved layout configurations."""

    desktop: DashboardBreakpointRead
    mobile: DashboardBreakpointRead


class DashboardDetail(StrictConfiguration):
    """Expose owner-saved dashboard configuration without runtime payload data."""

    id: UUID
    name: str
    revision: Revision
    created_at: datetime
    updated_at: datetime
    groups: list[DashboardGroupRead]
    instances: list[GadgetInstanceRead]
    layouts: DashboardLayoutsRead


class DashboardExportFence(StrictConfiguration):
    """Bind one dashboard projection to its parent revision and rendered config digest."""

    id: UUID
    created_at: datetime
    updated_at: datetime
    revision: Revision
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DashboardExportPage(StrictConfiguration):
    """Return a bounded owner dashboard page and exact projection fences."""

    owner_id: int = Field(ge=1)
    record_kind: Literal["dashboards"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    items: list[DashboardDetail] = Field(max_length=100)
    fences: list[DashboardExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: Literal["definition_changed_after_snapshot"] | None = None


class DashboardExportValidation(StrictConfiguration):
    """Report whether dashboard projections and the fixed-cutoff inventory remain unchanged."""

    valid: bool
    reason: Literal["valid", "owner_unavailable", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class GadgetDefinitionExport(StrictConfiguration):
    """Expose one saved renderer definition without placement dependencies or runtime state."""

    id: UUID
    name: Name
    revision: Revision
    renderer: str
    source_ids: list[UUID] = Field(max_length=MAX_SOURCES_PER_DEFINITION)
    scope: GadgetScope
    filters: GadgetFilters
    highlight_rules: list[HighlightRule] = Field(max_length=MAX_RULES_PER_DEFINITION)
    created_at: datetime
    updated_at: datetime


class GadgetDefinitionExportFence(StrictConfiguration):
    """Bind a saved definition and its immutable saved-configuration projection for publication."""

    id: UUID
    created_at: datetime
    updated_at: datetime
    revision: Revision
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible: bool


class GadgetDefinitionExportPage(StrictConfiguration):
    """Return one bounded all-definition owner page, including definitions used by no dashboard."""

    owner_id: int = Field(ge=1)
    record_kind: Literal["gadget_definitions"]
    snapshot_at: datetime
    snapshot_count: int = Field(ge=0)
    omitted_count: int = Field(default=0, ge=0)
    items: list[GadgetDefinitionExport] = Field(max_length=100)
    fences: list[GadgetDefinitionExportFence] = Field(max_length=100)
    payload_bytes: int = Field(ge=0, le=16_777_216)
    max_payload_bytes: int = Field(default=16_777_216, ge=1, le=16_777_216)
    next_cursor: str | None = None
    available: bool = True
    omission_reason: Literal["definition_changed_after_snapshot"] | None = None


class GadgetDefinitionExportValidation(StrictConfiguration):
    """Report whether the cutoff inventory and exact saved definition projections still match."""

    valid: bool
    reason: Literal["valid", "snapshot_count_changed", "record_changed"]
    observed_snapshot_count: int = Field(ge=0)


class RendererRead(StrictConfiguration):
    """Describe renderer configuration, adapter presence and geometry without runtime guarantees.

    ``available`` means the production data adapter exists; it does not establish runtime or provider acceptance.
    """

    id: str
    config_version: int
    minimum_width: int
    minimum_height: int
    runtime_state: Literal["planned", "available"]
    capability_keys: list[str]


class PresetSlotRead(StrictConfiguration):
    """Expose one stable source-free preset slot identity and renderer configuration."""

    slot_id: str
    renderer: str


class DashboardPresetRead(StrictConfiguration):
    """Expose one stable preset catalog entry and its planned slots."""

    id: str
    label: str
    family: Literal["purpose", "variant"]
    slots: list[PresetSlotRead]


class PresetSourceRead(StrictConfiguration):
    """Identify a selected source generation/status resolved during preset preview."""

    id: UUID
    generation: int | None
    status: str


class PresetSlotPreview(StrictConfiguration):
    """Return normalized slot configuration, selected sources, and setup warnings."""

    slot_id: str
    renderer: str
    source_ids: list[UUID]
    scope: dict[str, object]
    filters: dict[str, object]
    highlight_rules: list[dict[str, object]]
    sources: list[PresetSourceRead]
    warnings: list[DashboardWarning]


class PresetLayoutItemRead(StrictConfiguration):
    """Return one proposed layout rectangle keyed by a stable preset slot ID."""

    slot_id: str
    x: int
    y: int
    w: int
    h: int


class PresetBreakpointRead(StrictConfiguration):
    """Expose preview columns and normalized proposed preset rectangles."""

    columns: int
    items: list[PresetLayoutItemRead]


class PresetLayoutsRead(StrictConfiguration):
    """Return proposed preset geometry for desktop and mobile breakpoints."""

    desktop: PresetBreakpointRead
    mobile: PresetBreakpointRead


class PresetPreviewRead(StrictConfiguration):
    """Expose canonical proposed configuration and fingerprint without persisting a preview token."""

    template_version: int
    preset_id: str
    name: str
    slots: list[PresetSlotPreview]
    target_dashboard_id: UUID | None
    target_revision: Revision | None
    layouts: PresetLayoutsRead
    preview_fingerprint: str


class HighlightPreviewRequest(StrictConfiguration):
    """Draft rules to dry-run against recent current evidence; nothing here is persisted."""

    source_ids: list[UUID] = Field(min_length=1, max_length=MAX_SOURCES_PER_DEFINITION)
    rules: list[HighlightRule] = Field(min_length=1, max_length=MAX_RULES_PER_DEFINITION)
    days: Annotated[StrictInt, Field(ge=1, le=7)] = 7
    source_item_ids: list[UUID] = Field(default_factory=list, max_length=100)  # the gadget's item scope

    @model_validator(mode="after")
    def ensure_distinct(self) -> "HighlightPreviewRequest":
        """Reject repeated sources or rule ids so per-rule counts stay unambiguous."""
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("source_ids must contain distinct values")
        GadgetConfiguration(highlight_rules=self.rules)
        return self


class HighlightPreviewRuleRead(StrictConfiguration):
    """Per-rule dry-run outcome including topics that no longer resolve."""

    rule_id: UUID
    match_count: int
    unresolved_topic_ids: list[UUID]


class HighlightPreviewRead(StrictConfiguration):
    """Bounded dry-run result; ``truncated`` means more recent evidence was not scanned."""

    window_days: int
    scanned: int
    truncated: bool
    matches: list[DashboardHighlightRead] = Field(max_length=100)
    rules: list[HighlightPreviewRuleRead]


class GadgetDefinitionUsageRead(StrictConfiguration):
    """One dashboard that places the definition (and so evaluates its rules)."""

    dashboard_id: UUID
    name: str
    instance_count: int
