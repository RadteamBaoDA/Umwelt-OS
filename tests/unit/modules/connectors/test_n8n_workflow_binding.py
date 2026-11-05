"""Tests for the helpers that bind a packaged n8n workflow to one Source (``build_workflow`` pieces)."""

from uuid import uuid4

import pytest

from modules.connectors import n8n
from modules.sources.schemas import ConnectorSource


def _source(source_type: str = "rss", **configuration: object) -> ConnectorSource:
    return ConnectorSource(
        workspace_id=uuid4(), local_only=False, id=uuid4(), type=source_type, status="active",
        generation=3, configuration=configuration,
    )


@pytest.mark.parametrize(("minutes", "expected"), [
    (15, {"field": "minutes", "minutesInterval": 15}),
    (360, {"field": "hours", "hoursInterval": 6, "triggerAtMinute": 0}),
    (1440, {"field": "days", "daysInterval": 1, "triggerAtHour": 0, "triggerAtMinute": 0}),
])
def test_schedule_rule_maps_supported_intervals(minutes: int, expected: dict[str, object]) -> None:
    assert n8n._schedule_rule(_source(schedule_interval_minutes=minutes)) == expected


def test_schedule_rule_falls_back_to_the_type_default_for_unsupported_values() -> None:
    fallback = n8n._schedule_rule(_source(schedule_interval_minutes=7))
    assert fallback == n8n._schedule_rule(_source())


def test_bind_placeholders_replaces_nested_strings_and_leaves_other_values() -> None:
    value = {"a": ["x-__SOURCE_ID__", 3, None], "b": {"c": "{{$env.SOURCE_ID}}/__BACKEND_REVISION__"}}
    bound = n8n._bind_placeholders(value, (("{{$env.SOURCE_ID}}", "S"), ("__SOURCE_ID__", "S"), ("__BACKEND_REVISION__", "2")))
    assert bound == {"a": ["x-S", 3, None], "b": {"c": "S/2"}}
    assert value["a"][0] == "x-__SOURCE_ID__"  # input is not mutated


def test_node_binding_sets_collector_and_rest_credentials() -> None:
    source = _source("api")
    collector = {"name": "Collect provider"}
    n8n._bind_node_credentials(collector, source, collector_credential_id="cc", manual_credential_id="mc", provider_credential_id=None)
    assert collector["credentials"]["httpHeaderAuth"] == {"id": "cc", "name": "Umwelt-OS source collector"}  # type: ignore[index]
    rest = {"name": "Fetch REST pages", "parameters": {"genericAuthType": "x"}, "credentials": {"old": 1}}
    n8n._bind_node_credentials(rest, source, collector_credential_id="cc", manual_credential_id="mc", provider_credential_id=None)
    assert rest == {"name": "Fetch REST pages", "parameters": {"authentication": "none"}}
    n8n._bind_node_credentials(rest, source, collector_credential_id="cc", manual_credential_id="mc", provider_credential_id="pc")
    assert rest["credentials"]["httpHeaderAuth"]["id"] == "pc"  # type: ignore[index]
    untouched = {"name": "Other"}
    n8n._bind_node_credentials(untouched, source, collector_credential_id="cc", manual_credential_id="mc", provider_credential_id="pc")
    assert untouched == {"name": "Other"}


def test_packaged_filename_rejects_unknown_types() -> None:
    with pytest.raises(ValueError, match="no packaged workflow"):
        n8n._packaged_workflow_filename(_source("unknown"))
    assert n8n._packaged_workflow_filename(_source("rss")) == "rss.json"
