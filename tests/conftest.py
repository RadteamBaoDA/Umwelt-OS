"""Root pytest conftest: test environment defaults only."""

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
