"""Shared fixtures for storage tests: an offline moto S3 server and an in-memory workspace fence."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from botocore.session import get_session
from moto.server import ThreadedMotoServer

from core.storage import WorkspaceStorageBusy
from core.storage.s3 import S3Config, S3StorageBackend

ACCESS_KEY = "test-access-key"
SECRET_KEY = "test-secret-key"  # moto-only credential


class MemoryFence:
    """Test ``StorageFence`` with the production semantics: exclusive nonblocking hold and a permanent erased flag."""

    def __init__(self) -> None:
        """Start with no holders and no erased workspaces."""
        self.held: set[UUID] = set()
        self.erased: set[UUID] = set()

    def hold(self, workspace_id: UUID, *, erase: bool = False) -> Any:
        """Unsupported (async only), like ``PostgresStorageFence``."""
        raise NotImplementedError

    @asynccontextmanager
    async def ahold(self, workspace_id: UUID, *, erase: bool = False) -> AsyncIterator[None]:
        """Take the workspace fence: busy if held, erased flag set (and kept) by erase callers."""
        if workspace_id in self.held:
            raise WorkspaceStorageBusy("Workspace physical storage is still active")
        if erase:
            self.erased.add(workspace_id)
        elif workspace_id in self.erased:
            raise WorkspaceStorageBusy("Workspace physical storage was erased")
        self.held.add(workspace_id)
        try:
            yield
        finally:
            self.held.discard(workspace_id)


@pytest.fixture
def memory_fence() -> MemoryFence:
    """Return a fresh in-memory workspace fence."""
    return MemoryFence()


@pytest.fixture(scope="session")
def moto_endpoint() -> Iterator[str]:
    """Run one in-process moto S3 server for the session and yield its endpoint URL."""
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_bucket(moto_endpoint: str) -> str:
    """Create and return a fresh uniquely named bucket on the moto server."""
    name = f"test-{uuid4().hex[:16]}"
    get_session().create_client(
        "s3", endpoint_url=moto_endpoint, region_name="us-east-1",
        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
    ).create_bucket(Bucket=name)
    return name


@pytest.fixture
def make_s3(moto_endpoint: str, s3_bucket: str) -> Any:
    """Return a builder of ``S3StorageBackend`` bound to the fresh bucket; keyword args override ``S3Config`` fields."""

    def build(fence: MemoryFence | None = None, **overrides: Any) -> S3StorageBackend:
        """Build a backend (default: new ``MemoryFence``) against the moto bucket."""
        config = S3Config(
            bucket=s3_bucket, endpoint_url=moto_endpoint, access_key_id=ACCESS_KEY, secret_access_key=SECRET_KEY,
            **overrides,
        )
        return S3StorageBackend(config, fence or MemoryFence())

    return build
