"""Root pytest conftest: test environment defaults only."""

import os

os.environ.setdefault("BBD_ENVIRONMENT", "test")
os.environ.setdefault("BBD_DATA_DIR", "./tmp_test_data")
os.environ.setdefault("TEST_PUBLIC_ORIGIN", "http://localhost:3300")
