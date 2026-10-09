"""Owner-local fictional conversation fixture for the explicit P12 demo seed."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.demo_seed import p12_demo_seed_id
from core.workspaces.schemas import Scope
from modules.chat.models import Conversation


async def ensure_demo_conversation(
    session: AsyncSession, *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[int, int, int]:
    """Create a stable demo conversation only when durable history consent is currently enabled.

    Returns (created, existing, skipped_for_privacy). The helper shares the coordinator transaction;
    the Memory privacy lock is retained through its commit, and disabled consent is never changed.
    """
    del multi_workspace_enabled  # history consent is owner-global; the flag only matters to scoped Source/Document seeds
    from modules.chat.worker import is_history_storage_enabled

    if not await is_history_storage_enabled(session, scope):
        return 0, 0, 1
    conversation_id = p12_demo_seed_id("conversation", "orchard-catalogue-planning")
    if await session.scalar(select(Conversation.id).where(
        Conversation.workspace_id == scope.workspace_id, Conversation.id == conversation_id,
    )) is not None:
        return 0, 1, 0
    session.add(Conversation(
        id=conversation_id,
        workspace_id=scope.workspace_id,
        actor_user_id=getattr(scope, "actor_user_id", None) or scope.user_id,
        title="Demo: plan the orchard lantern catalogue",
        metadata_json={"demo_namespace": "bbd-os.demo.phase-12"},
        ephemeral=False,
    ))
    await session.flush()
    return 1, 0, 0
