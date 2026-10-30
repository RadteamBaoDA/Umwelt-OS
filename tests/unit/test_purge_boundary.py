"""Boundary check: ingestion must not reach into other modules' private retention data."""

import ast
from pathlib import Path

FORBIDDEN = (
    "modules.connectors.provider_specs",
    "modules.knowledge.documents.models",
    "modules.knowledge.observations.models",
    "modules.sources.models",
)


def test_purge_expired_datasource_data_uses_public_contracts_only():
    """purge_expired_datasource_data has no function-local imports of foreign private modules."""
    tree = ast.parse(Path("modules/ingestion/public.py").read_text(encoding="utf-8"))
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "purge_expired_datasource_data"
    )
    imported = [n.module for n in ast.walk(func) if isinstance(n, ast.ImportFrom)]
    assert not [m for m in imported if m in FORBIDDEN]
    names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    assert not names & {"Observation", "Document", "DocumentVersion", "DocumentInteraction", "Source"}
