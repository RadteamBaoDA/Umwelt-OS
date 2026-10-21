"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from core.erasure_sql import AccountErasureOwner

from . import models as _models  # noqa: F401 - register this owner's tables
from . import topics as _topics  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "news_observations",
    "news_recovery_checkpoints",
    "news_stories",
    "news_story_identities",
    "news_topics",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)
