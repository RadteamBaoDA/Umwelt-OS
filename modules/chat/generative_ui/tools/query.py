"""Structured tabular and chart dataset query, aggregation, and grounding engine."""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CitationRef(BaseModel):
    """Grounding citation metadata attached to a table row or chart data point."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    document_id: str
    chunk_id: str
    document_version_id: str | None = None
    title: str | None = None


class TableColumn(BaseModel):
    """Column definition for a structured Generative UI table."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    align: Literal["left", "right", "center"] = "left"


class TablePayload(BaseModel):
    """Structured table payload compatible with A2UI Table and Umwelt-OS citation grounding."""

    model_config = ConfigDict(extra="forbid")

    surface_id: str = "umwelt-table"
    catalog_id: str = "copilotkit://open-generative-ui-tables"
    title: str = Field(min_length=1, max_length=255)
    columns: list[TableColumn] = Field(min_length=1, max_length=16)
    rows: list[list[str]] = Field(default_factory=list, max_length=200)
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    citations: list[CitationRef] = Field(default_factory=list, max_length=100)
    source: str = Field(default="Grounded evidence", max_length=255)
    is_sample: bool = False


class ChartSeriesConfig(BaseModel):
    """Configuration for one plotted series in a Recharts / Generative UI chart."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    color_token: str = Field(default="var(--chart-1)", max_length=64)


class ChartPayload(BaseModel):
    """Structured chart payload preserving Umwelt-OS design tokens and grounding citations."""

    model_config = ConfigDict(extra="forbid")

    chart_type: Literal["bar", "pie", "line", "scatter", "distribution"] = "bar"
    title: str = Field(min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    x_key: str = Field(default="label", min_length=1, max_length=64)
    series: list[ChartSeriesConfig] = Field(min_length=1, max_length=8)
    data: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    citations: list[CitationRef] = Field(default_factory=list, max_length=100)
    source: str = Field(default="Grounded evidence", max_length=255)
    unit: str | None = Field(default=None, max_length=32)
    is_sample: bool = False


def _extract_citation_ref(obj: Any) -> CitationRef | None:
    """Extract a `CitationRef` from a dict or `EvidenceItem` if provenance IDs are present."""
    if isinstance(obj, Mapping):
        source_id = obj.get("source_id") or obj.get("sourceId")
        document_id = obj.get("document_id") or obj.get("documentId")
        chunk_id = obj.get("chunk_id") or obj.get("chunkId")
        doc_ver_id = obj.get("document_version_id") or obj.get("documentVersionId")
        title = obj.get("title")
    else:
        source_id = getattr(obj, "source_id", None) or getattr(obj, "sourceId", None)
        document_id = getattr(obj, "document_id", None) or getattr(obj, "documentId", None)
        chunk_id = getattr(obj, "chunk_id", None) or getattr(obj, "chunkId", None)
        doc_ver_id = getattr(obj, "document_version_id", None) or getattr(obj, "documentVersionId", None)
        title = getattr(obj, "title", None)
    if source_id and document_id and chunk_id:
        return CitationRef(
            source_id=str(source_id),
            document_id=str(document_id),
            chunk_id=str(chunk_id),
            document_version_id=str(doc_ver_id) if doc_ver_id else None,
            title=str(title) if title else None,
        )
    return None


def _to_float(val: Any) -> float | None:
    """Safely parse a numeric value for aggregation."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    if isinstance(val, str):
        cleaned = val.replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def query_records(
    records: Sequence[Mapping[str, Any]],
    *,
    filters: Mapping[str, Any] | None = None,
    group_by: str | None = None,
    metric: str | None = None,
    agg: Literal["sum", "avg", "count", "min", "max"] = "sum",
    sort_by: str | None = None,
    descending: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter, group, aggregate, and sort structured records while preserving citation provenance.

    Args:
        records: Input sequence of record mappings.
        filters: Optional exact-match or collection-membership filter mapping.
        group_by: Optional field name to group records by.
        metric: Optional numeric field name to aggregate when `group_by` is set.
        agg: Aggregation function ('sum', 'avg', 'count', 'min', 'max').
        sort_by: Optional field name to sort results by.
        descending: Whether to sort in descending order.
        limit: Maximum number of records to return (bounded to 1..200).

    Returns:
        List of new record dictionaries with `source_id`, `document_id`, and `chunk_id` preserved.
    """
    bounded_limit = max(1, min(int(limit), 200))
    filtered: list[dict[str, Any]] = []
    for raw in records:
        row = dict(raw)
        if filters:
            matched = True
            for k, expected in filters.items():
                actual = row.get(k)
                if isinstance(expected, (list, tuple, set, frozenset)):
                    if actual not in expected:
                        matched = False
                        break
                elif str(actual).lower() != str(expected).lower():
                    matched = False
                    break
            if not matched:
                continue
        filtered.append(row)

    if group_by:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in filtered:
            key_val = str(row.get(group_by, "Other"))
            groups.setdefault(key_val, []).append(row)

        aggregated: list[dict[str, Any]] = []
        for group_val, group_rows in groups.items():
            nums = [
                n
                for r in group_rows
                if metric is not None and (n := _to_float(r.get(metric))) is not None
            ]
            if agg == "count" or metric is None:
                agg_value: float | int = len(group_rows)
            elif not nums:
                agg_value = 0.0
            elif agg == "sum":
                agg_value = round(sum(nums), 4)
            elif agg == "avg":
                agg_value = round(sum(nums) / len(nums), 4)
            elif agg == "min":
                agg_value = min(nums)
            elif agg == "max":
                agg_value = max(nums)
            else:
                agg_value = round(sum(nums), 4)

            out_key = metric or "count"
            merged_row: dict[str, Any] = {
                group_by: group_val,
                out_key: agg_value,
            }
            for r in group_rows:
                ref = _extract_citation_ref(r)
                if ref is not None:
                    merged_row.setdefault("source_id", ref.source_id)
                    merged_row.setdefault("document_id", ref.document_id)
                    merged_row.setdefault("chunk_id", ref.chunk_id)
                    if ref.document_version_id:
                        merged_row.setdefault("document_version_id", ref.document_version_id)
                    break
            aggregated.append(merged_row)
        filtered = aggregated

    if sort_by:
        def _sort_key(item: dict[str, Any]) -> tuple[int, float | str]:
            val = item.get(sort_by)
            num = _to_float(val)
            if num is not None:
                return (1, num)
            return (0, str(val or ""))

        filtered.sort(key=_sort_key, reverse=descending)

    return filtered[:bounded_limit]


_PAIR_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{1,36}?)\s*[:=-]\s*\$?(-?\d[\d,]*(?:\.\d+)?)\s*(%|ms|s|k|M|B|USD|EUR|VND)?"
)


def _extract_records_from_evidence_or_text(
    items: Sequence[Any],
    answer_text: str,
) -> tuple[list[dict[str, Any]], list[CitationRef], bool]:
    """Extract tabular/chart records and grounding citations from evidence items or answer text."""
    records: list[dict[str, Any]] = []
    citations: list[CitationRef] = []
    seen_chunks: set[str] = set()

    for idx, item in enumerate(items, 1):
        ref = _extract_citation_ref(item)
        if ref and ref.chunk_id not in seen_chunks:
            seen_chunks.add(ref.chunk_id)
            citations.append(ref)

        if isinstance(item, Mapping) and not hasattr(item, "content") and "content" not in item:
            records.append(dict(item))
            continue

        content = (
            str(item.get("content", ""))
            if isinstance(item, Mapping)
            else str(getattr(item, "content", ""))
        )
        title = (
            str(item.get("title", f"Source {idx}"))
            if isinstance(item, Mapping)
            else str(getattr(item, "title", f"Source {idx}"))
        )
        matches = _PAIR_RE.findall(content)
        if matches:
            for label, num_str, unit in matches[:6]:
                val = _to_float(num_str)
                if val is None:
                    continue
                row: dict[str, Any] = {
                    "label": label.strip(),
                    "value": val,
                    "unit": unit or "",
                    "source": title,
                }
                if ref:
                    row["source_id"] = ref.source_id
                    row["document_id"] = ref.document_id
                    row["chunk_id"] = ref.chunk_id
                records.append(row)
        elif content.strip():
            row = {
                "label": title[:60],
                "value": len(content.split()),
                "summary": " ".join(content.strip().split())[:140],
            }
            if ref:
                row["source_id"] = ref.source_id
                row["document_id"] = ref.document_id
                row["chunk_id"] = ref.chunk_id
            records.append(row)

    if not records and answer_text:
        for label, num_str, unit in _PAIR_RE.findall(answer_text)[:8]:
            val = _to_float(num_str)
            if val is not None:
                row = {"label": label.strip(), "value": val, "unit": unit or ""}
                if citations:
                    row["source_id"] = citations[0].source_id
                    row["document_id"] = citations[0].document_id
                    row["chunk_id"] = citations[0].chunk_id
                records.append(row)

    # No canned sample fallback: an empty result stays empty so callers can decline to render.
    return records, citations, False


def build_table_payload(
    title: str,
    records_or_evidence: Sequence[Any] = (),
    *,
    columns: Sequence[str | TableColumn] | None = None,
    source: str | None = None,
    prompt: str = "",
    answer_text: str = "",
) -> TablePayload:
    """Build a structured `TablePayload` preserving grounding citations and A2UI catalog format.

    Args:
        title: Table heading.
        records_or_evidence: Sequence of record dicts or `EvidenceItem` objects.
        columns: Optional explicit column keys or `TableColumn` definitions.
        source: Provenance label displayed in the table footer.
        prompt: User request text.
        answer_text: Assistant response text.

    Returns:
        Validated `TablePayload` instance.
    """
    records, citations, is_sample = _extract_records_from_evidence_or_text(
        records_or_evidence, answer_text or prompt
    )
    hidden_keys = {"source_id", "document_id", "document_version_id", "chunk_id"}
    if columns:
        resolved_cols = [
            col if isinstance(col, TableColumn) else TableColumn(key=col, label=col.replace("_", " ").title())
            for col in columns
        ]
    else:
        first_keys = [k for k in (records[0].keys() if records else ["label", "value"]) if k not in hidden_keys]
        if not first_keys:
            first_keys = ["label", "value"]
        resolved_cols = [
            TableColumn(
                key=k,
                label=k.replace("_", " ").title(),
                align="right" if isinstance(records[0].get(k), (int, float)) else "left",
            )
            for k in first_keys[:8]
        ]

    rows: list[list[str]] = []
    for rec in records:
        rows.append([str(rec.get(col.key, "")) for col in resolved_cols])

    resolved_source = source or (
        "Illustrative sample dataset" if is_sample else "Grounded workspace evidence"
    )
    return TablePayload(
        title=title.strip()[:255] or "Comparison Table",
        columns=resolved_cols,
        rows=rows,
        records=records,
        citations=citations,
        source=resolved_source,
        is_sample=is_sample,
    )


def build_chart_payload(
    title: str,
    chart_type: Literal["bar", "pie", "line", "scatter", "distribution"] = "bar",
    records_or_evidence: Sequence[Any] = (),
    *,
    x_key: str = "label",
    series: Sequence[ChartSeriesConfig] | None = None,
    source: str | None = None,
    prompt: str = "",
    answer_text: str = "",
) -> ChartPayload:
    """Build a structured `ChartPayload` with Umwelt-OS `--chart-1..5` tokens and citations.

    Args:
        title: Chart title.
        chart_type: Visual chart type ('bar', 'pie', 'line', 'scatter', 'distribution').
        records_or_evidence: Sequence of record dicts or `EvidenceItem` objects.
        x_key: Category/domain key in each data point.
        series: Optional explicit series definitions.
        source: Provenance label.
        prompt: User request text.
        answer_text: Assistant response text.

    Returns:
        Validated `ChartPayload` instance.
    """
    records, citations, is_sample = _extract_records_from_evidence_or_text(
        records_or_evidence, answer_text or prompt
    )
    if series:
        resolved_series = list(series)
    else:
        sample_row = records[0] if records else {"label": "Item", "value": 0}
        numeric_keys = [
            k
            for k, v in sample_row.items()
            if k != x_key
            and k not in {"source_id", "document_id", "document_version_id", "chunk_id"}
            and _to_float(v) is not None
        ]
        if not numeric_keys:
            numeric_keys = ["value"]
        resolved_series = [
            ChartSeriesConfig(
                key=k,
                label=k.replace("_", " ").title(),
                color_token=f"var(--chart-{(idx % 5) + 1})",
            )
            for idx, k in enumerate(numeric_keys[:5])
        ]

    resolved_source = source or (
        "Illustrative sample dataset" if is_sample else "Grounded workspace evidence"
    )
    return ChartPayload(
        chart_type=chart_type,
        title=title.strip()[:255] or "Data Visualization",
        x_key=x_key if any(x_key in r for r in records) else next(iter(records[0].keys()), "label"),
        series=resolved_series,
        data=records,
        citations=citations,
        source=resolved_source,
        is_sample=is_sample,
    )
