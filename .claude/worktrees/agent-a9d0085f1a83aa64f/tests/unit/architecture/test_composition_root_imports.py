"""The descriptor-only composition root must not load module implementations (no eager facade imports)."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV = {**os.environ, "ENVIRONMENT": "test", "TEST_PUBLIC_ORIGIN": "http://localhost:3300"}


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter rooted at the repository so import state is never shared."""
    return subprocess.run(  # noqa: S603 - fixed interpreter, test-owned code string
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_core_modules_import_does_not_load_heavy_module_implementations() -> None:
    """``import core.modules`` reads descriptors only: no ingestion/sources/connectors facade or worker is loaded."""
    result = _run(
        "import sys, core.modules\n"
        "heavy = ['modules.ingestion.public', 'modules.ingestion._runs', 'modules.ingestion.worker',\n"
        "         'modules.sources.worker', 'modules.sources.public', 'modules.connectors.public',\n"
        "         'modules.connectors.service.access', 'modules.knowledge.documents.public']\n"
        "loaded = [m for m in heavy if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    assert result.returncode == 0, result.stderr


def test_api_and_worker_entry_points_still_import() -> None:
    """Dropping the eager facade imports must not break the composed API or worker entry points."""
    result = _run("import apps.api.main, apps.worker.main")
    assert result.returncode == 0, result.stderr


def test_cycle_prone_modules_import_first_in_a_fresh_interpreter() -> None:
    """Each module must import cleanly as the FIRST import (guards ingestion/connectors facade cycles)."""
    for module in (
        "modules.ingestion._purge",
        "modules.connectors.service.collection_requests",
        "modules.connectors.service.config",
        "modules.connectors.service.packaged",
        "modules.connectors.service.schemas",
        "modules.connectors.service.scope",
    ):
        result = _run(f"import {module}")
        assert result.returncode == 0, f"{module}: {result.stderr[-600:]}"
