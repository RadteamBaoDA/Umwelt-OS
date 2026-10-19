"""Unit tests for query_records, and grounded table/chart payload builders."""

from modules.chat.generative_ui.tools.query import (
    build_chart_payload,
    build_table_payload,
    query_records,
)


def test_query_records_filters_aggregates_and_preserves_citations() -> None:
    """Verify query_records filters, groups, sorts, and preserves citation IDs."""
    records = [
        {
            "category": "Compute",
            "region": "US",
            "spend": 120.0,
            "source_id": "src-1",
            "document_id": "doc-1",
            "chunk_id": "chk-1",
        },
        {
            "category": "Compute",
            "region": "US",
            "spend": 80.0,
            "source_id": "src-1",
            "document_id": "doc-1",
            "chunk_id": "chk-2",
        },
        {
            "category": "Storage",
            "region": "EU",
            "spend": 50.0,
            "source_id": "src-2",
            "document_id": "doc-2",
            "chunk_id": "chk-3",
        },
    ]
    grouped = query_records(
        records,
        group_by="category",
        metric="spend",
        agg="sum",
        sort_by="spend",
        descending=True,
    )
    assert len(grouped) == 2
    assert grouped[0]["category"] == "Compute"
    assert grouped[0]["spend"] == 200.0
    assert grouped[0]["source_id"] == "src-1"
    assert grouped[0]["document_id"] == "doc-1"
    assert grouped[0]["chunk_id"] == "chk-1"


def test_build_table_and_chart_payloads_preserve_grounding_citations() -> None:
    """Verify build_table_payload and build_chart_payload extract data and retain citations."""
    evidence = [
        {
            "source_id": "11111111-1111-1111-1111-111111111111",
            "document_id": "22222222-2222-2222-2222-222222222222",
            "chunk_id": "33333333-3333-3333-3333-333333333333",
            "title": "Q3 Financial Report",
            "content": "Compute: $45,000\nStorage: $18,500\nNetwork: $9,200",
        }
    ]
    table = build_table_payload("Q3 Infrastructure Spend", evidence)
    assert table.is_sample is False
    assert len(table.rows) == 3
    assert len(table.citations) == 1
    assert table.citations[0].source_id == "11111111-1111-1111-1111-111111111111"
    assert table.citations[0].document_id == "22222222-2222-2222-2222-222222222222"
    assert table.citations[0].chunk_id == "33333333-3333-3333-3333-333333333333"

    chart = build_chart_payload("Q3 Spend Breakdown", chart_type="bar", records_or_evidence=evidence)
    assert chart.chart_type == "bar"
    assert chart.is_sample is False
    assert chart.series[0].color_token == "var(--chart-1)"
    assert len(chart.citations) == 1
    assert chart.citations[0].chunk_id == "33333333-3333-3333-3333-333333333333"
