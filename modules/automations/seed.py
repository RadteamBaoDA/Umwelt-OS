"""Disabled, fictional example automation rules for the explicit demo seed (never run at startup)."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.demo_seed import demo_seed_id
from modules.automations import public
from modules.automations.schemas import AutomationCreate

# Every example installs disabled; enabling is an explicit owner action (PATCH enabled=true).
# Enabling the brief example transfers the daily_brief schedule slot from the internal cron.
EXAMPLES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("new-document-alert", {
        "name": "Example: tell me about new documents",
        "trigger": {"type": "new_document"},
        "actions": [{"type": "create_notification", "message": "A new document finished indexing.", "link": "/app"}],
    }),
    ("task-due-reminder", {
        "name": "Example: remind me before a task is due",
        "trigger": {"type": "task_due", "lead_minutes": 60},
        "actions": [{"type": "create_notification", "message": "A task is due within the hour.", "link": "/app"}],
    }),
    ("weekday-brief", {
        "name": "Example: weekday morning brief",
        "trigger": {"type": "schedule", "cron": "0 8 * * 1-5", "timezone": "Asia/Ho_Chi_Minh"},
        "actions": [{"type": "generate_brief", "scope": "daily"}],
    }),
    ("weekly-agent-digest", {
        "name": "Example: weekly planning check (owner approves each run)",
        "trigger": {"type": "schedule", "cron": "0 9 * * 1", "timezone": "Asia/Ho_Chi_Minh"},
        "actions": [{
            "type": "run_agent", "profile_id": "planning",
            "instruction": "Summarize the orchard lantern goals and which milestones look at risk.",
        }],
    }),
)


async def ensure_demo_automations(
    session: AsyncSession, owner_id: int, registry: Mapping[str, Any], settings: Settings,
) -> int:
    """Create the disabled example rules with stable ids inside the caller's transaction; return the count.

    The caller holds the P10 demo-seed receipt, so edited or deleted examples are never recreated.
    """
    for key, body in EXAMPLES:
        await public.create_automation(
            session, owner_id, AutomationCreate(**body, enabled=False), registry, settings,
            automation_id=demo_seed_id("automation", key), commit=False,
        )
    return len(EXAMPLES)
