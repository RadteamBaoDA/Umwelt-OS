"""Owner profile contracts and immutable configuration snapshots for specialist runs."""

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.model_gateway.schemas import AIExecutionConfig
from core.tools import ToolRegistry, ToolRisk
from core.workspaces.schemas import Scope
from modules.agents.access import actor, admit
from modules.agents.handoff import HANDOFF_TOOL
from modules.agents.internal_writes import (
    AUTOMATION_PROFILE_TOOLS,
    INTERNAL_PROFILE_TOOLS,
    INTERNAL_WRITE_PROFILES,
    is_internal_write,
)
from modules.agents.models import AgentProfile, AgentProfileRevision
from modules.agents.schemas import AgentProfilePatch, AgentProfileRead

PROFILE_TITLES = {
    "supervisor": "Supervisor", "knowledge": "Knowledge", "research": "Research",
    "personal": "Personal", "project": "Project", "news": "News",
    "planning": "Planning", "automation": "Automation",
}
DEFAULT_PROMPTS = {
    "supervisor": "Coordinate the owner's request using the approved read-only tools. Delegate at most once, and only through agents.handoff called by itself; if it is unavailable or refused, handle the request directly and explain unavailable capabilities.",
    "knowledge": "Answer questions grounded in the owner's authorized knowledge sources. Cite only evidence returned by the registered tools and say when evidence is insufficient.",
    "research": "Research the owner's question using only the currently registered tools and selected sources. Do not imply that browser research or external search is available unless its tool is present.",
    "personal": "Help the owner reason about personal information in selected knowledge sources and about the owner's tasks and goals. Do not invent tasks, goals, reminders, or private records that an authorized tool did not return; task and goal changes need the owner's approval.",
    "project": "Help with project information found in selected knowledge sources. Do not present generic documents as structured project records when no project owner tool is registered.",
    "news": "Answer news-related questions only from sources actually returned by registered tools. Do not fabricate headlines or imply live news coverage when its adapter is unavailable.",
    "planning": "Help the owner plan from available evidence and the owner's tasks and goals. Any task or goal change is only a proposal: request the write tool and wait for the owner's approval card; never claim a change happened before it is approved.",
    "automation": "Help the owner design automation rules (trigger, conditions, actions). Use automations.list to avoid duplicates, then propose a rule with automations.create and wait for the owner approval card; an approved proposal is saved DISABLED and only the owner can enable it. Never claim a rule is active, and do not propose webhook triggers or unlisted webhook aliases.",
}
AUTOMATION_PROFILE_ID = "automation"
PROFILE_LIMITS = {
    "max_steps": 20, "max_tool_calls": 10, "max_active_seconds": 300,
    "max_browser_jobs": 2, "max_browser_pages": 6, "max_browser_bytes": 10 * 1024 * 1024,
}
NATIVE_READ_TOOLS = frozenset({
    "knowledge.get_document", "knowledge.list_documents", "search.query",
    "sources.list_sources", "sources.get_source", "github.list_project_events",
})
# Offered by default only to the Project specialist and the Supervisor that routes to it.
PROJECT_ONLY_TOOL = "github.list_project_events"
SPECIALIST_GATES = {"research": "browser.read"}
DOMAIN_UNAVAILABLE = {
    "project": "project_owner_tools_unavailable",
    "news": "news_owner_tools_unavailable",
}


def _canonical_json(value: object) -> bytes:
    """Encode snapshot values deterministically for request and profile identity digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _tool_contracts(registry: ToolRegistry, workspace_id: UUID) -> dict[str, dict[str, str]]:
    """Expose only registered non-confirmed read tools and the established webhook approval contract."""
    result: dict[str, dict[str, str]] = {}
    hides = registry.hides_tool
    for definition in registry.list_tools():
        if hides is not None and hides(definition.name, workspace_id):
            continue
        if definition.risk == ToolRisk.READ_ONLY and not definition.confirmation_required or ((definition.name == "webhook.send" and definition.risk == ToolRisk.EXTERNAL_WRITE
               and definition.confirmation_required) or is_internal_write(definition)):
            result[definition.name] = {
                "name": definition.name, "version": definition.version,
                "fingerprint": definition.schema_fingerprint,
            }
    return result


def _snapshot(
    row: AgentProfile | None, profile_id: str, registry: ToolRegistry, workspace_id: UUID,
) -> dict[str, Any]:
    """Build the secret-free profile view, retaining exact registry fingerprints as authority ceilings."""
    contracts = _tool_contracts(registry, workspace_id)
    defaults = NATIVE_READ_TOOLS - ({PROJECT_ONLY_TOOL} if profile_id not in {"project", "supervisor"} else frozenset())
    defaults = (defaults | ({HANDOFF_TOOL} if profile_id == "supervisor" else frozenset())
                | (INTERNAL_PROFILE_TOOLS if profile_id in INTERNAL_WRITE_PROFILES else frozenset())
                | (AUTOMATION_PROFILE_TOOLS if profile_id == AUTOMATION_PROFILE_ID else frozenset()))
    selected = row.allowed_tools if row is not None else [
        contracts[name] for name in sorted(defaults) if name in contracts
    ]
    unavailable: list[str] = []
    valid_tools = [item for item in selected if isinstance(item, dict)
                   and item.get("name") in contracts
                   and contracts[item["name"]] == item]
    gate = SPECIALIST_GATES.get(profile_id)
    if gate is not None and gate not in {item["name"] for item in valid_tools}:
        unavailable.append("bounded_browser_unavailable")
    if profile_id == "supervisor" and HANDOFF_TOOL not in {item["name"] for item in valid_tools}:
        unavailable.append("bounded_supervisor_handoff_unavailable")
    if profile_id in DOMAIN_UNAVAILABLE:
        unavailable.append(DOMAIN_UNAVAILABLE[profile_id])
    profile_enabled = row.enabled if row is not None else True
    if not valid_tools:
        unavailable.append("registered_read_tools_unavailable")
    capability = "unavailable" if not valid_tools or not profile_enabled else "partial" if unavailable else "available"
    return {
        "id": profile_id, "title": PROFILE_TITLES[profile_id], "enabled": profile_enabled,
        "revision": row.revision if row is not None else 0,
        "model_alias": row.model_alias if row is not None else "reasoning-large",
        "prompt": row.prompt if row is not None else DEFAULT_PROMPTS[profile_id],
        "allowed_tools": valid_tools,
        "available_tools": [item for name, item in sorted(contracts.items())
                             if (name != "webhook.send" or profile_id in {"supervisor", "research"})
                             and (name not in INTERNAL_PROFILE_TOOLS or profile_id in INTERNAL_WRITE_PROFILES)
                             and (name not in AUTOMATION_PROFILE_TOOLS or profile_id == AUTOMATION_PROFILE_ID)
                             and (name != HANDOFF_TOOL or profile_id == "supervisor")],
        "source_ids": list(row.source_ids) if row is not None else [],
        "capability": capability, "unavailable_reasons": sorted(set(unavailable)),
        "limits": PROFILE_LIMITS,
    }


def _read_profile(value: dict[str, object]) -> AgentProfileRead:
    """Validate a prepared profile snapshot before returning it through the public DTO."""
    return AgentProfileRead.model_validate(value)


def _profile_content(value: AgentProfileRead) -> dict[str, object]:
    """Keep only owner-edited profile content in its immutable revision hash."""
    return value.model_dump(mode="json", exclude={"available_tools", "capability", "unavailable_reasons"})


async def list_profiles(
    session: AsyncSession, registry: ToolRegistry, config: AIExecutionConfig,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[AgentProfileRead, ...]:
    """List the fixed specialist roster of the admitted workspace and mark unavailable aliases or adapters.

    Owner admission precedes the query; profile rows are bound to ``scope.workspace_id``.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    rows = (await session.scalars(select(AgentProfile).where(
        AgentProfile.workspace_id == scope.workspace_id,
    ))).all()
    by_id = {row.profile_id: row for row in rows}
    profiles = []
    for profile_id in PROFILE_TITLES:
        value = _snapshot(by_id.get(profile_id), profile_id, registry, scope.workspace_id)
        alias = value["model_alias"]
        if alias not in config.aliases or not config.aliases[alias].model:
            value["unavailable_reasons"] = sorted({*value["unavailable_reasons"], "model_alias_unconfigured"})
            value["capability"] = "unavailable"
        else:
            value["unavailable_reasons"] = sorted({*value["unavailable_reasons"], "model_tool_capability_unverified"})
            if value["capability"] == "available":
                value["capability"] = "partial"
        profiles.append(_read_profile(value))
    return tuple(profiles)


async def get_profile(
    session: AsyncSession, profile_id: str,
    registry: ToolRegistry, config: AIExecutionConfig,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentProfileRead:
    """Return one known fixed-roster profile or a non-enumerating not-found response.

    Owner admission precedes the query; the profile row is read within the admitted workspace.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled)
    if profile_id not in PROFILE_TITLES:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    row = await session.scalar(select(AgentProfile).where(
        AgentProfile.workspace_id == scope.workspace_id, AgentProfile.profile_id == profile_id,
    ))
    value = _snapshot(row, profile_id, registry, scope.workspace_id)
    alias = value["model_alias"]
    if alias not in config.aliases or not config.aliases[alias].model:
        value["unavailable_reasons"] = sorted({*value["unavailable_reasons"], "model_alias_unconfigured"})
        value["capability"] = "unavailable"
    else:
        value["unavailable_reasons"] = sorted({*value["unavailable_reasons"], "model_tool_capability_unverified"})
        if value["capability"] == "available":
            value["capability"] = "partial"
    return _read_profile(value)


async def update_profile_in_uow(
    session: AsyncSession, profile_id: str, patch: AgentProfilePatch,
    registry: ToolRegistry, config: AIExecutionConfig,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> AgentProfileRead:
    """Write one optimistic profile revision into the caller's transaction without committing it.

    The access fence is locked first (before the profile lock and any Source lock); the caller
    commits with the same fence via ``commit_with_replay``.
    """
    await admit(session, scope=scope, multi_workspace_enabled=multi_workspace_enabled, lock=True)
    if profile_id not in PROFILE_TITLES:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    owner_id = actor(scope)
    workspace_id = scope.workspace_id
    # A row lock cannot serialize concurrent creation while this profile has no row yet.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": int.from_bytes(hashlib.sha256(f"profile:{workspace_id}:{profile_id}".encode()).digest()[:8], "big", signed=True)},
    )
    row = await session.scalar(select(AgentProfile).where(
        AgentProfile.workspace_id == workspace_id, AgentProfile.profile_id == profile_id,
    ).with_for_update())
    current_revision = row.revision if row is not None else 0
    if patch.expected_revision != current_revision:
        raise HTTPException(status_code=409, detail="Agent profile changed; reload before saving")
    if len(patch.prompt.encode("utf-8")) > 32_000:
        raise HTTPException(status_code=413, detail="Agent prompt exceeds the size limit")
    if patch.model_alias not in config.aliases:
        raise HTTPException(status_code=422, detail="Model alias is not configured")
    contracts = _tool_contracts(registry, scope.workspace_id)
    chosen = [item.model_dump(mode="json") for item in patch.allowed_tools]
    if len({item["name"] for item in chosen}) != len(chosen) or any(contracts.get(item["name"]) != item for item in chosen):
        raise HTTPException(status_code=422, detail="Selected tool contract is no longer available")
    if "webhook.send" in {item["name"] for item in chosen} and profile_id not in {"supervisor", "research"}:
        raise HTTPException(status_code=422, detail="This profile cannot request external actions")
    if {item["name"] for item in chosen} & INTERNAL_PROFILE_TOOLS and profile_id not in INTERNAL_WRITE_PROFILES:
        raise HTTPException(status_code=422, detail="This profile cannot use task or goal tools")
    if HANDOFF_TOOL in {item["name"] for item in chosen} and profile_id != "supervisor":
        raise HTTPException(status_code=422, detail="Only the Supervisor profile may delegate")
    if {item["name"] for item in chosen} & AUTOMATION_PROFILE_TOOLS and profile_id != AUTOMATION_PROFILE_ID:
        raise HTTPException(status_code=422, detail="This profile cannot use automation tools")
    if patch.source_ids:
        from modules.sources.public import list_tool_sources

        selected_source_ids = frozenset(patch.source_ids)
        sources = await list_tool_sources(
            session, scope=scope, multi_workspace_enabled=multi_workspace_enabled,
            limit=len(selected_source_ids), cursor=None,
            source_ids=selected_source_ids, owner_all=False,
        )
        if {item.id for item in sources.items} != selected_source_ids:
            raise HTTPException(status_code=422, detail="Source scope contains an inactive source")
    revision = current_revision + 1
    if row is None:
        row = AgentProfile(workspace_id=workspace_id, profile_id=profile_id, owner_id=owner_id)
        session.add(row)
    row.enabled = patch.enabled
    row.model_alias = patch.model_alias
    row.prompt = patch.prompt
    row.allowed_tools = chosen
    row.source_ids = [str(item) for item in patch.source_ids]
    row.revision = revision
    profile_view = _snapshot(row, profile_id, registry, scope.workspace_id)
    snapshot = _profile_content(_read_profile(profile_view))
    snapshot_hash = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    session.add(AgentProfileRevision(
        workspace_id=workspace_id, profile_id=profile_id, owner_id=owner_id, revision=revision,
        snapshot=snapshot, snapshot_hash=snapshot_hash,
    ))
    await session.flush()
    return _read_profile(profile_view)


async def resolve_profile_snapshot(
    session: AsyncSession, profile_id: str, expected_revision: int,
    registry: ToolRegistry, config: AIExecutionConfig,
    *, scope: Scope, multi_workspace_enabled: bool,
) -> tuple[dict[str, Any], str]:
    """Resolve a selected enabled revision and bind its exact current tool/model contracts before enqueue."""
    profile = await get_profile(
        session, profile_id, registry, config,
        scope=scope, multi_workspace_enabled=multi_workspace_enabled,
    )
    if profile.revision != expected_revision:
        raise HTTPException(status_code=409, detail="Agent profile changed; reload before starting")
    if not profile.enabled or profile.capability == "unavailable":
        raise HTTPException(status_code=503, detail="Agent profile capability is unavailable")
    contracts = _tool_contracts(registry, scope.workspace_id)
    profile_snapshot = _profile_content(profile)
    digest = hashlib.sha256(_canonical_json(profile_snapshot)).hexdigest()
    snapshot = profile.model_dump(mode="json")
    snapshot.pop("available_tools", None)
    snapshot["profile_revision_hash"] = digest
    if not snapshot["allowed_tools"] or any(contracts.get(item["name"]) != item for item in snapshot["allowed_tools"]):
        raise HTTPException(status_code=503, detail="Agent profile tools are unavailable")
    mapping = config.aliases.get(profile.model_alias)
    if mapping is None or not mapping.model:
        raise HTTPException(status_code=503, detail="Agent profile model alias is unavailable")
    # Pin only identity and policy metadata; the gateway secret never enters the run snapshot.
    snapshot["gateway"] = {
        "configuration_revision": config.configuration_revision,
        "gateway_identity": config.gateway_identity,
        "destination_id": config.endpoint_destination_id,
        "model_alias": profile.model_alias,
        "model": mapping.model,
        "model_version": mapping.version,
        "model_destination": mapping.destination,
        "privacy": config.privacy.model_dump(mode="json"),
    }
    snapshot["run_snapshot_hash"] = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    return snapshot, digest
