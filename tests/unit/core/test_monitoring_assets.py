"""Monitoring assets stay consistent with the metrics the application actually exports."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from core import metrics

ROOT = Path(__file__).resolve().parents[3]
MONITORING = ROOT / "infrastructure" / "monitoring"
_SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


def _exported_names() -> set[str]:
    """Return every sample-name stem the registry can expose, with and without histogram/counter suffixes."""
    names: set[str] = set()
    for family in metrics.REGISTRY.collect():
        names.add(family.name)
        names.update(family.name + suffix for suffix in _SUFFIXES)  # labelled families have no samples until used
        names.update(sample.name for sample in family.samples)
    return names


def _referenced_names(text: str) -> set[str]:
    """Return every ``umwelt_*`` identifier used in a PromQL-bearing document."""
    return set(re.findall(r"\bumwelt_[a-z0-9_]+", text))


def test_alert_rules_parse_and_reference_exported_metrics() -> None:
    """Every alert has an expression, a runbook anchor and only references metrics that exist."""
    document = yaml.safe_load((MONITORING / "alerts.yml").read_text())
    rules = [rule for group in document["groups"] for rule in group["rules"]]
    assert {rule["alert"] for rule in rules} >= {
        "UmweltApi5xxRateHigh", "UmweltApiLatencyP95High", "UmweltQueueDepthHigh", "UmweltQueueOldestJobTooOld",
        "UmweltOutboxLagHigh", "UmweltCronJobFailing", "UmweltCronJobStale", "UmweltDbPoolSaturated",
        "UmweltModelBreakerOpen", "UmweltEventLoopLagHigh",
    }
    exported = _exported_names()
    docs = (ROOT / "docs" / "observability.md").read_text().lower()
    for rule in rules:
        assert rule["expr"].strip() and rule["annotations"]["runbook"].startswith("docs/observability.md#")
        assert f"### {rule['alert'].lower()}" in docs, f"missing runbook heading for {rule['alert']}"
        for name in _referenced_names(rule["expr"]):
            assert name in exported, f"{rule['alert']} references unknown metric {name}"


def test_dashboard_is_valid_json_and_references_exported_metrics() -> None:
    """The provisioned dashboard parses, has panels and only queries metrics that exist."""
    text = (MONITORING / "grafana" / "dashboards" / "umwelt-overview.json").read_text()
    dashboard = json.loads(text)
    assert dashboard["uid"] == "umwelt-overview" and len(dashboard["panels"]) >= 15
    exported = _exported_names()
    for name in _referenced_names(text):
        assert name in exported, f"dashboard references unknown metric {name}"


def test_prometheus_config_and_compose_overlay_are_loopback_only() -> None:
    """Prometheus and Grafana publish on 127.0.0.1 only, behind the monitoring profile."""
    prom = yaml.safe_load((MONITORING / "prometheus.yml").read_text())
    assert prom["rule_files"] == ["/etc/prometheus/alerts.yml"]
    assert {job["job_name"] for job in prom["scrape_configs"]} == {"umwelt-api", "umwelt-worker"}
    compose = yaml.safe_load((ROOT / "docker-compose.monitoring.yml").read_text())
    for service in ("prometheus", "grafana"):
        definition = compose["services"][service]
        assert definition["profiles"] == ["monitoring"]
        assert all(str(port).startswith("127.0.0.1:") for port in definition["ports"])
