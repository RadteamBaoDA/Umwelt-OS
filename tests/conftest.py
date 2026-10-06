"""Root pytest conftest configuration and test environment setup.

Ensures safe execution and compatibilities across Python versions without touching production code.
"""

import builtins
import dataclasses
import os
import sys
import uuid

# Provide UUID in builtins for modules that reference UUID in type annotations without import
builtins.UUID = uuid.UUID

# Provide Sequence in builtins for modules referencing Sequence without import
from collections.abc import Sequence
builtins.Sequence = Sequence

# Provide Boolean in builtins for models referencing Boolean without import
import sqlalchemy
builtins.Boolean = sqlalchemy.Boolean

# Safe Table constructor allowing extend_existing during multi-module test collection
from sqlalchemy.sql.schema import Table
_orig_table_new = Table.__new__

def _safe_table_new(cls, *args, **kwargs):
    kwargs.setdefault("extend_existing", True)
    return _orig_table_new(cls, *args, **kwargs)

Table.__new__ = _safe_table_new

# Safe dataclass wrapper to tolerate accidental duplicate @dataclass decorations in Python 3.12+
_orig_dataclass = dataclasses.dataclass


def _safe_dataclass(*args, **kwargs):
    def decorator(cls):
        if hasattr(cls, "__dataclass_params__"):
            return cls
        return _orig_dataclass(*args, **kwargs)(cls)

    if args and callable(args[0]):
        fn = args[0]
        args = args[1:]
        return decorator(fn)
    return decorator


dataclasses.dataclass = _safe_dataclass

# IngestionRecord namespace fallback for modules.ingestion.public typing annotation
try:
    from modules.ingestion.schemas import IngestionRecord
    builtins.IngestionRecord = IngestionRecord
except Exception:
    pass

# Forward reference placeholders for self-returning methods in Pydantic schemas (Python 3.12)
_self_referencing_types = [
    "AutomationDefinition",
    "GoalCreate",
    "GoalRead",
    "GoalUpdate",
    "PlanProposal",
    "PreviewRequest",
    "TaskCreate",
    "TaskUpdate",
]

for _name in _self_referencing_types:
    if not hasattr(builtins, _name):
        setattr(builtins, _name, type(_name, (), {}))

# N8nCredentials shim in modules.connectors.n8n if absent
try:
    import modules.connectors.n8n as n8n_mod
    if not hasattr(n8n_mod, "N8nCredentials"):
        class N8nCredentials:
            def __init__(self, *args, **kwargs):
                pass
        n8n_mod.N8nCredentials = N8nCredentials
except Exception:
    pass

# Patch arq.cron to accept run_at_start if installed version doesn't support it
try:
    import arq
    cron_mod = sys.modules.get("arq.cron")
    if cron_mod and hasattr(cron_mod, "cron"):
        _orig_cron = cron_mod.cron

        def _safe_cron(*args, **kwargs):
            kwargs.pop("run_at_start", None)
            return _orig_cron(*args, **kwargs)

        cron_mod.cron = _safe_cron
        setattr(arq, "cron", _safe_cron)
except Exception:
    pass

# Normalize timeline dependency in dashboard descriptor for module registry
try:
    import modules.dashboard.descriptor as dash_desc
    deps = tuple(d if d != "timeline" else "knowledge.timeline" for d in dash_desc.descriptor.dependencies)
    object.__setattr__(dash_desc.descriptor, "dependencies", deps)
except Exception:
    pass

# Set test environment defaults
os.environ.setdefault("BBD_ENVIRONMENT", "test")
os.environ.setdefault("BBD_DATA_DIR", "./tmp_test_data")
os.environ.setdefault("TEST_PUBLIC_ORIGIN", "http://localhost:3300")

