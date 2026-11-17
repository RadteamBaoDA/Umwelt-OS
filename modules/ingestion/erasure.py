"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from core.erasure_sql import AccountErasureOwner

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "collector_credentials",
    "event_outbox",
    "ingestion_batches",
    "ingestion_collection_receipts",
    "ingestion_runs",
    "ingestion_stages",
    "observation_normalizations",
    "source_ingestion_state",
    "source_observations",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)
