"""Finite account-erasure worker contracts; constructing a scope grants no authority."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AccountErasureScope:
    """Bind cleanup to one persisted deletion operation and its original account/workspace.

    Owners must call ``authorize_account_erasure`` before writes. This is not an active
    account, browser session or general-purpose bypass of workspace authorization.
    """

    operation_id: UUID
    account_id: int
    workspace_id: UUID


@dataclass(frozen=True)
class ErasureProgress:
    """Return a resumable cleanup checkpoint after its effects have committed.

    ``complete`` means all app-controlled domain data is gone; Auth still owns final
    identity removal. Cursors contain identifiers only, never content or credentials.
    """

    phase: str
    cursor: dict[str, Any] = field(default_factory=dict)
    complete: bool = False
