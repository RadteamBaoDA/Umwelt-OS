"""Shared owner admission stub for Documents unit tests (no DB)."""

from unittest.mock import AsyncMock, patch

import pytest

from tests.unit.modules.knowledge.documents._scope import FENCE


@pytest.fixture(autouse=True)
def admission():
    """Replace the workspace owner admission read; yield the mock so tests can assert on it."""
    mock = AsyncMock(return_value=FENCE)
    with patch("modules.knowledge.documents.public.read_access_fence", mock):
        yield mock
