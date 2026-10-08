"""ConnectorSource carries local_only so ingestion and connector fences can read it."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from modules.sources.public import _connector_source
from modules.sources.schemas import ConnectorSource


@pytest.mark.parametrize("flag", [True, False])
def test_connector_source_projection_round_trips_local_only(flag):
    row = SimpleNamespace(
        id=uuid4(), workspace_id=uuid4(), type="connector", status="active", generation=1,
        configuration={}, provider="rss", local_only=flag,
    )
    projected = _connector_source(row)
    assert projected.local_only is flag
    # the exact ingestion revalidation expression
    assert (projected.status != "active" or projected.local_only) is flag


def test_connector_source_requires_local_only():
    with pytest.raises(ValidationError):
        ConnectorSource(id=uuid4(), workspace_id=uuid4(), type="c", status="active", generation=1, configuration={})
