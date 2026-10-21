"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from core.erasure_sql import AccountErasureOwner

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "entities",
    "entity_alias_evidence",
    "entity_aliases",
    "entity_correction_decisions",
    "entity_evidence_memberships",
    "entity_extraction_results",
    "entity_extraction_work",
    "entity_field_evidence",
    "entity_owner_actions",
    "entity_redirects",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)
