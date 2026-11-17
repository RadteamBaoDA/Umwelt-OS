"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from core.erasure_sql import AccountErasureOwner

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "chat_agent_activity_links",
    "chat_conversations",
    "chat_message_mutation_receipts",
    "chat_messages",
    "chat_response_runs",
    "chat_stream_events",
    "chat_templates",
    "chat_todos",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)
