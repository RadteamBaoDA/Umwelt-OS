"""Every export dataset provider accepts the scope kwargs the route passes (A2 extends this)."""

import inspect

import pytest

from modules.export.routes import DATASETS


@pytest.mark.parametrize("dataset,record_kind,public", DATASETS, ids=[d[0] for d in DATASETS])
def test_provider_accepts_scope_kwargs(dataset, record_kind, public):
    for name in ("export_page", "validate_export_fences"):
        params = inspect.signature(getattr(public, name)).parameters
        assert "scope" in params and "multi_workspace_enabled" in params, (dataset, name)
