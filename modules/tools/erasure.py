"""Account deletion table ownership for this module.

The authorized erasure coordinator uses these exact tables and ORM metadata to
delete bounded workspace rows in foreign-key order. No caller may infer authority
from this inventory; it must validate the persisted deletion operation first.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth.deletion_service import authorize_account_erasure
from core.auth.erasure_contracts import AccountErasureScope
from core.erasure_sql import AccountErasureOwner
from core.remote_heavy_models import RemoteHeavyGuard
from modules.tools.models import BrowserReadJob

from . import models as _models  # noqa: F401 - register this owner's tables

ERASURE_TABLES: tuple[str, ...] = (
    "browser_page_evidence",
    "browser_read_jobs",
    "mcp_capabilities",
    "mcp_capability_grants",
    "mcp_connections",
    "mcp_discoveries",
    "mcp_inbound_clients",
)

ERASURE_OWNER = AccountErasureOwner(ERASURE_TABLES)


async def erase_account_remote_guards(session: AsyncSession, scope: AccountErasureScope) -> bool:
    """Remove at most 100 browser remote guards after their hard runtime bounds expire.

    A live guard means a browser process may still write evidence; the required stage retries
    after its bound. Browser jobs remain as exact ownership anchors until this stage commits.
    """
    await authorize_account_erasure(session, scope)
    rows = (await session.execute(select(RemoteHeavyGuard.operation_id, RemoteHeavyGuard.state,
                                        RemoteHeavyGuard.expires_at).join(
        BrowserReadJob, BrowserReadJob.operation_id == RemoteHeavyGuard.operation_id,
    ).where(BrowserReadJob.workspace_id == scope.workspace_id).limit(100))).all()
    if any(state in {"active", "uncertain"} and expiry > datetime.now(UTC)
           for _, state, expiry in rows):
        raise RuntimeError("Browser remote work is still within its hard runtime bound")
    if rows:
        await session.execute(delete(RemoteHeavyGuard).where(
            RemoteHeavyGuard.operation_id.in_(operation_id for operation_id, _, _ in rows),
        ))
    return len(rows) < 100
