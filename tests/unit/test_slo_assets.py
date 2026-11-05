"""Guards the SLO documentation and k6 assets so docs, thresholds and scripts cannot drift apart."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = ROOT / "tests/load/lib/thresholds.js"
SLO_DOC = ROOT / "docs/slo.md"
REPORT = ROOT / "docs/performance-report.md"


def _threshold_metrics() -> set[str]:
    """Return the metric names (tag selectors removed) that tests/load/lib/thresholds.js constrains."""
    source = THRESHOLDS.read_text(encoding="utf-8")
    body = source.split("export const sloThresholds", 1)[1]
    keys = re.findall(r"^\s*(?:'([^']+)'|([a-z_]+)):\s*\[", body, flags=re.MULTILINE)
    return {re.sub(r"\{.*\}$", "", quoted or bare) for quoted, bare in keys}


def test_every_threshold_metric_is_documented_in_slo_doc_and_report() -> None:
    """Each k6 threshold metric appears in docs/slo.md and in the performance report's mapping table."""
    metrics = _threshold_metrics()
    assert {
        "http_req_failed",
        "http_req_duration",
        "chat_ttft_ms",
        "ingest_lag_ms",
        "ingest_queue_age_ms",
    } <= metrics
    slo, report = SLO_DOC.read_text(encoding="utf-8"), REPORT.read_text(encoding="utf-8")
    for metric in sorted(metrics):
        assert metric in slo, f"{metric} missing from docs/slo.md"
        assert metric in report, f"{metric} missing from docs/performance-report.md"


def test_load_test_runner_is_disposable_and_pinned() -> None:
    """The runner uses a unique test project, tears it down, pins the k6 image and is wired into Make and dev.ps1."""
    script = (ROOT / "scripts/load-test.sh").read_text(encoding="utf-8")
    assert "bbd-os-test-" in script and "down --volumes --remove-orphans" in script
    assert re.search(r"grafana/k6:\d+\.\d+\.\d+", script), "k6 image must be pinned to a version"
    assert "docker-compose.test.yml" in script
    assert re.search(
        r"^load-test:\n\tbash scripts/load-test\.sh", (ROOT / "Makefile").read_text(), re.MULTILINE
    )
    assert "'load-test'" in (ROOT / "scripts/dev.ps1").read_text(encoding="utf-8")


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to stub-load the k6 scripts"
)
def test_k6_scripts_load_with_stubbed_runtime() -> None:
    """scripts/check-k6-scripts.mjs imports every k6 script against stubs and validates scenarios/thresholds."""
    result = subprocess.run(  # noqa: S603  # test-only subprocess with fixed arguments
        ["node", str(ROOT / "scripts/check-k6-scripts.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # noqa: S607  # node from PATH is the test toolchain
    )
    assert result.returncode == 0, result.stderr
    assert "k6 scripts OK" in result.stdout
