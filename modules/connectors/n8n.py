import contextlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from uuid import UUID
from xml.etree import ElementTree

import httpx

from modules.connectors.backends import PROVIDER_SOURCE_TYPES, is_native_provider
from modules.connectors.public import (
    default_schedule_interval_minutes,
    overlap_floor,
    validate_public_url,
)
from modules.sources.schemas import ConnectorSource


def workflow_state(cursor: str | None) -> dict[str, str | None]:
    """Return the prior cursor and overlap floor used for bounded catch-up."""
    floor = overlap_floor(cursor)
    return {
        "cursor_before": cursor,
        "catch_up_since": floor.isoformat() if floor is not None else None,
    }


def workflow_name(source_id: UUID, operation_id: UUID) -> str:
    """Build a deterministic source-scoped name for one provisioning operation."""
    return f"BBD-OS connector {source_id} {operation_id.hex}"


def workflow_webhook_path(source_id: UUID, source_type: str) -> str:
    """Build the source-scoped manual webhook path for a supported connector type."""
    connector_type = {"api": "rest", "web": "url", "rss": "rss", "mcp": "mcp"}[source_type]
    return f"bbd-collect-{connector_type}-{source_id.hex}"


def build_workflow(
    source: ConnectorSource,
    *,
    desired_revision: int,
    backend_revision: int,
    workflow_operation_id: UUID,
    workflow_name_value: str | None = None,
    collector_credential_id: str,
    manual_credential_id: str,
    provider_credential_id: str | None,
) -> dict[str, Any]:
    """Bind source-fenced credentials and select a workflow by trusted stored provider.

    Generic backend capability does not authorize the provider-fetch envelope.
    GitHub and MCP retain their specialized workflows; registered world providers
    use provider-fetch with the same revision and credential binding as feeds.
    """
    native = is_native_provider(source.provider)
    if native and source.type != PROVIDER_SOURCE_TYPES[source.provider]:
        raise ValueError("Source type does not match the registered provider")
    filename = (
        "mcp.json" if source.type == "mcp" else
        "github.json" if source.provider == "github" else
        "provider-fetch.json" if native else
        {"rss": "rss.json", "web": "url.json", "api": "rest.json"}.get(source.type)
    )
    if filename is None:
        raise ValueError("This source type has no packaged workflow")
    path = Path(__file__).resolve().parents[2] / "infrastructure" / "n8n" / "workflows" / filename
    workflow = deepcopy(json.loads(path.read_text(encoding="utf-8")))
    source_id = str(source.id)
    collector_names = {
        "Read bounded RSS / Atom pages",
        "Validate source",
        "Validate source and load cursor",
        "Collect bounded pages",
        "Submit acknowledged batch",
        "Acknowledge no changes",
        "Collect provider",
        "Collect GitHub repository",
        "Collect MCP source",
    }
    for node in workflow["nodes"]:
        if node.get("name") == "Schedule":
            configured_interval = source.configuration.get("schedule_interval_minutes")
            interval = (
                configured_interval
                if configured_interval in {15, 30, 60, 360, 1440}
                else default_schedule_interval_minutes(source.type)
            )
            if interval < 60:
                schedule = {"field": "minutes", "minutesInterval": interval}
            elif interval < 1440:
                schedule = {
                    "field": "hours",
                    "hoursInterval": interval // 60,
                    "triggerAtMinute": 0,
                }
            else:
                schedule = {
                    "field": "days",
                    "daysInterval": 1,
                    "triggerAtHour": 0,
                    "triggerAtMinute": 0,
                }
            node["parameters"]["rule"]["interval"] = [schedule]
        elif node.get("name") == "Manual collection":
            node["parameters"]["path"] = workflow_webhook_path(source.id, source.type)
            node.setdefault("credentials", {}).setdefault("httpHeaderAuth", {})
            node["credentials"]["httpHeaderAuth"].update(
                {"id": manual_credential_id, "name": "BBD-OS manual trigger"}
            )
        elif node.get("name") in collector_names:
            node.setdefault("credentials", {}).setdefault("httpHeaderAuth", {})
            node["credentials"]["httpHeaderAuth"].update(
                {"id": collector_credential_id, "name": "BBD-OS MCP source collector" if source.type == "mcp" else "BBD-OS source collector"}
            )
        elif node.get("name") == "Fetch REST pages":
            if provider_credential_id:
                node["parameters"]["authentication"] = "genericCredentialType"
                node["parameters"]["genericAuthType"] = "httpHeaderAuth"
                node.setdefault("credentials", {})["httpHeaderAuth"] = {
                    "id": provider_credential_id,
                    "name": "BBD-OS provider credential",
                }
            else:
                node["parameters"]["authentication"] = "none"
                node["parameters"].pop("genericAuthType", None)
                node.pop("credentials", None)
    workflow["name"] = workflow_name_value or workflow_name(source.id, workflow_operation_id)
    workflow["settings"]["timezone"] = str(
        source.configuration.get("timezone", "Asia/Ho_Chi_Minh")
    )

    def bind_source_id(value: Any) -> Any:
        """Recursively replace packaged placeholders with this source's identifiers."""
        if isinstance(value, str):
            return (
                value.replace("{{$env.BBD_SOURCE_ID}}", source_id)
                .replace("__BBD_SOURCE_ID__", source_id)
                .replace("__BBD_SOURCE_GENERATION__", str(source.generation))
                .replace("__BBD_CONNECTOR_REVISION__", str(desired_revision))
                .replace("__BBD_BACKEND_REVISION__", str(backend_revision))
                .replace("__BBD_MCP_CONNECTION_ID__", str(source.configuration.get("connection_id", "")))
            )
        if isinstance(value, list):
            return [bind_source_id(item) for item in value]
        if isinstance(value, dict):
            return {key: bind_source_id(item) for key, item in value.items()}
        return value

    bound = bind_source_id(workflow)
    return {
        key: bound[key]
        for key in ("name", "nodes", "connections", "settings")
        if key in bound
    }


class N8nApi:
    """Access bounded n8n workflow management endpoints without ambient proxies."""

    def __init__(self, service_url: str, api_key: str) -> None:
        """Store the n8n service root and API key header."""
        self._base_url = service_url.rstrip("/")
        self._headers = {"X-N8N-API-KEY": api_key}

    async def find_workflows(self, name: str) -> list[dict[str, Any]]:
        """Find exact-name workflows through at most twenty paginated API pages."""
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(20):
            params: dict[str, str | int] = {"name": name, "limit": 250}
            if cursor is not None:
                params["cursor"] = cursor
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.get(
                    f"{self._base_url}/api/v1/workflows",
                    headers=self._headers,
                    params=params,
                )
                response.raise_for_status()
            payload = response.json()
            workflows = payload.get("data", [])
            if not isinstance(workflows, list):
                raise ValueError("n8n workflow lookup returned an invalid list")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
            matches.extend(
                workflow for workflow in workflows
                if isinstance(workflow, dict) and workflow.get("name") == name
            )
            cursor = payload.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
            if page == 19:
                raise ValueError("n8n workflow lookup exceeded its pagination bound")
        return matches

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Fetch one workflow and require an object response."""
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.get(
                f"{self._base_url}/api/v1/workflows/{workflow_id}",
                headers=self._headers,
            )
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("n8n workflow response is invalid")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
        return payload

    async def create_workflow(self, workflow: dict[str, Any]) -> str:
        """Create a workflow and return its nonempty n8n identifier."""
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/workflows",
                headers=self._headers,
                json=workflow,
            )
            response.raise_for_status()
        identifier = response.json().get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("n8n workflow create response omitted its ID")
        return identifier

    async def update_workflow(self, workflow_id: str, workflow: dict[str, Any]) -> None:
        """Replace a known workflow from the supplied packaged definition."""
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.put(
                f"{self._base_url}/api/v1/workflows/{workflow_id}",
                headers=self._headers,
                json=workflow,
            )
            response.raise_for_status()

    async def set_active(self, workflow_id: str, active: bool) -> None:
        """Activate or deactivate a workflow by its n8n identifier."""
        action = "activate" if active else "deactivate"
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/workflows/{workflow_id}/{action}",
                headers=self._headers,
            )
            response.raise_for_status()


def workflow_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Check stable public workflow identity before associating a recovered create ID."""
    if expected.get("name") != actual.get("name"):
        return False
    expected_nodes = expected.get("nodes")
    actual_nodes = actual.get("nodes")
    if not isinstance(expected_nodes, list) or not isinstance(actual_nodes, list):
        return False
    nodes_by_name = {
        node.get("name"): node for node in actual_nodes
        if isinstance(node, dict) and isinstance(node.get("name"), str)
    }
    if len(nodes_by_name) != len(actual_nodes) or len(nodes_by_name) != len(expected_nodes):
        return False
    for wanted in expected_nodes:
        if not isinstance(wanted, dict):
            return False
        found = nodes_by_name.get(wanted.get("name"))
        if not isinstance(found, dict):
            return False
        for key in ("type", "typeVersion", "parameters"):
            if wanted.get(key) != found.get(key):
                return False
        if wanted.get("credentials", {}) != found.get("credentials", {}):
            return False
    if expected.get("connections") != actual.get("connections"):
        return False
    expected_settings = expected.get("settings", {})
    actual_settings = actual.get("settings", {})
    if not isinstance(expected_settings, dict) or not isinstance(actual_settings, dict):
        return False
    return all(actual_settings.get(key) == value for key, value in expected_settings.items())


async def read_rss(
    url: str, cursor: str | None, *, fetch: Callable[[str], Awaitable[bytes]] | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Fetch bounded RSS/Atom pages with URL checks, overlap filtering, and normalized records.

    ``fetch`` (native collection) replaces the built-in client with the caller's pinned, gated
    transport that returns one body per call. ``stats["resume_url"]`` is set when a page/record cap
    stopped the walk at a page boundary with a continuation link left (exact continuation), and
    ``stats["truncated"]`` when unread items remain inside a page (no exact continuation exists),
    so the caller never advances the cursor over unread data.
    """
    from modules.connectors.registry import normalize

    if fetch is None:
        await validate_public_url(url)
    floor = overlap_floor(cursor)
    visited: set[str] = set()
    records: list[dict[str, object]] = []
    cursor_times: list[str] = []
    total_bytes = 0
    current_url: str | None = url
    async with contextlib.AsyncExitStack() as stack:
        client = None if fetch is not None else await stack.enter_async_context(
            httpx.AsyncClient(timeout=15, follow_redirects=False))
        for _ in range(10):
            if current_url is None or current_url in visited:
                break
            if fetch is None:
                await validate_public_url(current_url)
            visited.add(current_url)
            if fetch is not None:
                body = bytearray(await fetch(current_url))
                total_bytes += len(body)
                if total_bytes > 25 * 1024 * 1024:
                    raise ValueError("RSS pagination exceeded the 25 MiB limit")
            else:
              assert client is not None
              async with client.stream("GET", current_url, headers={"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("RSS redirect has no location")
                    current_url = urljoin(current_url, location)
                    await validate_public_url(current_url)
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > 25 * 1024 * 1024:
                        raise ValueError("RSS pagination exceeded the 25 MiB limit")
                    body.extend(chunk)
            root = ElementTree.fromstring(bytes(body))
            def text(element: ElementTree.Element, names: set[str]) -> str:
                """Return descendant text for the first matching local XML tag name."""
                for child in element.iter():
                    if child.tag.rsplit("}", 1)[-1].lower() in names:
                        return "".join(child.itertext()).strip()
                return ""

            page_items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
            for position, item in enumerate(page_items):
                identifier = text(item, {"guid", "id", "link"})
                title = text(item, {"title"})
                content = text(item, {"encoded", "content", "summary", "description"}) or title
                raw_date = text(item, {"published", "pubdate", "date"})
                raw_updated = text(item, {"updated"})
                observed_at = datetime.now(UTC)
                def parse_feed_time(raw_value: str) -> datetime | None:
                    """Parse ISO or RFC feed time and normalize naive values as UTC."""
                    if not raw_value:
                        return None
                    try:
                        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))  # noqa: FURB162  # keeps exact parsing of 'Z' suffix; fromisoformat(Z) is not strictly equivalent
                    except ValueError:
                        try:
                            parsed = parsedate_to_datetime(raw_value)
                        except (TypeError, ValueError):
                            return None
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    return parsed.astimezone(UTC)

                published_at = parse_feed_time(raw_date)
                updated_at = parse_feed_time(raw_updated)
                cursor_at = published_at or updated_at or observed_at
                link = next(
                    (str(node.attrib.get("href")) for node in item.iter()
                     if node.tag.rsplit("}", 1)[-1].lower() == "link" and node.attrib.get("href")),
                    text(item, {"link"}),
                )
                canonical_url = urljoin(current_url, link) if link else None
                if floor is None or cursor_at >= floor:
                    cursor_times.append(cursor_at.isoformat())
                    records.append(
                        normalize(
                            {
                                "provider_id": identifier or url,
                                "content": content[:4_000],
                                "observed_at": datetime.now(UTC).isoformat(),
                                "version": (raw_date or raw_updated or None),
                                "metadata": {
                                    "title": title[:500],
                                    "canonical_url": canonical_url,
                                    "published_at": published_at.isoformat() if published_at else None,
                                },
                            }
                        )
                    )
                if len(records) == 500:
                    if stats is not None and position < len(page_items) - 1:
                        stats["truncated"] = True  # unread items remain on this page
                    break

            next_link = next(
                (node.attrib.get("href") for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "link" and node.attrib.get("rel", "").lower() == "next"),
                None,
            )
            current_url = urljoin(current_url, next_link) if next_link else None
            if len(records) >= 500:
                if stats is not None and current_url is not None and "truncated" not in stats:
                    stats["resume_url"] = current_url  # record cap met exactly at a page boundary: exact continuation
                break
        else:
            if stats is not None and current_url is not None and current_url not in visited:
                stats["resume_url"] = current_url  # ten-page cap reached at a boundary with a link left
    return {
        "cursor_before": cursor,
        "cursor_after": max(cursor_times, default=cursor),
        "records": records,
    }
