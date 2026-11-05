import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID
from xml.etree import ElementTree

import httpx

from core.config import Settings
from modules.connectors.backends import PROVIDER_SOURCE_TYPES, is_native_provider
from modules.connectors.provider_specs import min_interval_minutes
from modules.connectors.providers.news import safe_xml_root
from modules.connectors.public import (
    default_schedule_interval_minutes,
    overlap_floor,
    validate_public_url,
)
from modules.connectors.selection import SKEW
from modules.sources.public import ConnectorSource

# Fixed in-network address packaged workflows call back to (every infrastructure/n8n/workflows/*.json URL).
# Not owner-configurable: a different value would desynchronize the shipped templates.
CALLBACK_BASE = "http://api:8000"


def n8n_service_identity(value: str) -> str:
    """Normalize a credential-free service root without guessing DNS aliases or changing its path.

    Remote IDs are scoped to this exact endpoint. Reject URLs carrying credentials, query
    parameters or fragments so endpoint lineage never persists secrets or ambiguous targets.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Invalid n8n service identity")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid n8n service identity")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", ""))


def workflow_state(cursor: str | None) -> dict[str, str | None]:
    """Return the prior cursor and overlap floor used for bounded catch-up."""
    floor = overlap_floor(cursor)
    return {
        "cursor_before": cursor,
        "catch_up_since": floor.isoformat() if floor is not None else None,
    }


def workflow_name(source_id: UUID, operation_id: UUID) -> str:
    """Build a deterministic source-scoped name for one provisioning operation."""
    return f"Umwelt-OS connector {source_id} {operation_id.hex}"


_CURRENT_WORKFLOW_PREFIX = "Umwelt-OS connector "
_LEGACY_WORKFLOW_PREFIX = "BBD-OS connector "


def connector_workflow_names(source_id: UUID) -> tuple[str, str]:
    """Return the (current, legacy) display names of a source's connector workflow."""
    return (f"{_CURRENT_WORKFLOW_PREFIX}{source_id}", f"{_LEGACY_WORKFLOW_PREFIX}{source_id}")


def alternate_workflow_name(name: str) -> str | None:
    """Swap the current and legacy brand prefix of a workflow name; None if it has neither."""
    for prefix, other in ((_CURRENT_WORKFLOW_PREFIX, _LEGACY_WORKFLOW_PREFIX), (_LEGACY_WORKFLOW_PREFIX, _CURRENT_WORKFLOW_PREFIX)):
        if name.startswith(prefix):
            return other + name[len(prefix):]
    return None


async def find_workflows_by_name(api: "N8nApi", name: str) -> list[dict[str, Any]]:
    """Find workflows by exact name, retrying the alternate brand prefix when none match.

    Installs created before the Umwelt-OS rename keep legacy "BBD-OS connector" names in n8n,
    while new rows may only know the current name (or vice versa); both resolve here. The
    alternate is tried only on an empty result so a current match is never mixed with a legacy one.
    """
    matches = await api.find_workflows(name)
    alternate = alternate_workflow_name(name)
    if not matches and alternate is not None:
        matches = await api.find_workflows(alternate)
    return matches


def workflow_webhook_paths(source_id: UUID, source_type: str) -> tuple[str, str]:
    """Return the (current, legacy) manual webhook paths for a supported connector type."""
    connector_type = {"api": "rest", "web": "url", "rss": "rss", "mcp": "mcp"}[source_type]
    return (f"umwelt-collect-{connector_type}-{source_id.hex}", f"bbd-collect-{connector_type}-{source_id.hex}")


def workflow_webhook_path(source_id: UUID, source_type: str) -> str:
    """Build the source-scoped manual webhook path used by newly built workflows."""
    return workflow_webhook_paths(source_id, source_type)[0]


_COLLECTOR_NODE_NAMES = frozenset({
    "Read bounded RSS / Atom pages",
    "Validate source",
    "Validate source and load cursor",
    "Collect bounded pages",
    "Request collection admission",
    "Submit acknowledged batch",
    "Acknowledge no changes",
    "Collect provider",
    "Collect GitHub repository",
    "Collect MCP source",
})


def _packaged_workflow_filename(source: ConnectorSource) -> str:
    """Select the packaged workflow file for a Source from its trusted stored type and provider.

    Raises ``ValueError`` when a native provider's Source type disagrees with its registration
    or when the type has no packaged workflow.
    """
    native = is_native_provider(source.provider)
    if native and source.type != PROVIDER_SOURCE_TYPES[str(source.provider)]:
        raise ValueError("Source type does not match the registered provider")
    filename = (
        "mcp.json" if source.type == "mcp" else
        "github.json" if source.provider == "github" else
        "provider-fetch.json" if native else
        {"rss": "rss.json", "web": "url.json", "api": "rest.json"}.get(source.type)
    )
    if filename is None:
        raise ValueError("This source type has no packaged workflow")
    return filename


def _schedule_rule(source: ConnectorSource) -> dict[str, Any]:
    """Return the n8n schedule interval rule for a Source's configured collection interval.

    An unsupported configured value falls back to the type default, and the provider minimum
    always applies; sub-hour intervals use minutes, sub-day hours, otherwise a daily trigger.
    """
    configured_interval = source.configuration.get("schedule_interval_minutes")
    interval = (
        configured_interval
        if configured_interval in {15, 30, 60, 360, 1440}
        else default_schedule_interval_minutes(source.type)
    )
    interval = max(interval, min_interval_minutes(source.provider))
    if interval < 60:
        return {"field": "minutes", "minutesInterval": interval}
    if interval < 1440:
        return {"field": "hours", "hoursInterval": interval // 60, "triggerAtMinute": 0}
    return {"field": "days", "daysInterval": 1, "triggerAtHour": 0, "triggerAtMinute": 0}


def _bind_node_credentials(
    node: dict[str, Any], source: ConnectorSource, *, collector_credential_id: str,
    manual_credential_id: str, provider_credential_id: str | None,
) -> None:
    """Mutate one packaged workflow node in place with this Source's schedule or credential binding.

    Only the nodes named Schedule, Manual collection, a collector node or Fetch REST pages are
    touched; every other node is left unchanged.
    """
    name = node.get("name")
    if name == "Schedule":
        node["parameters"]["rule"]["interval"] = [_schedule_rule(source)]
    elif name == "Manual collection":
        node["parameters"]["path"] = workflow_webhook_path(source.id, source.type)
        node.setdefault("credentials", {}).setdefault("httpHeaderAuth", {})
        node["credentials"]["httpHeaderAuth"].update(
            {"id": manual_credential_id, "name": "Umwelt-OS manual trigger"}
        )
    elif name in _COLLECTOR_NODE_NAMES:
        node.setdefault("credentials", {}).setdefault("httpHeaderAuth", {})
        node["credentials"]["httpHeaderAuth"].update(
            {"id": collector_credential_id, "name": "Umwelt-OS MCP source collector" if source.type == "mcp" else "Umwelt-OS source collector"}
        )
    elif name == "Fetch REST pages":
        if provider_credential_id:
            node["parameters"]["authentication"] = "genericCredentialType"
            node["parameters"]["genericAuthType"] = "httpHeaderAuth"
            node.setdefault("credentials", {})["httpHeaderAuth"] = {
                "id": provider_credential_id,
                "name": "Umwelt-OS provider credential",
            }
        else:
            node["parameters"]["authentication"] = "none"
            node["parameters"].pop("genericAuthType", None)
            node.pop("credentials", None)


def _bind_placeholders(value: Any, replacements: tuple[tuple[str, str], ...]) -> Any:
    """Recursively replace packaged placeholder strings, in order, throughout a JSON-like value."""
    if isinstance(value, str):
        for placeholder, replacement in replacements:
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [_bind_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _bind_placeholders(item, replacements) for key, item in value.items()}
    return value


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
    filename = _packaged_workflow_filename(source)
    path = Path(__file__).resolve().parents[2] / "infrastructure" / "n8n" / "workflows" / filename
    workflow = deepcopy(json.loads(path.read_text(encoding="utf-8")))
    source_id = str(source.id)
    for node in workflow["nodes"]:
        _bind_node_credentials(
            node, source, collector_credential_id=collector_credential_id,
            manual_credential_id=manual_credential_id, provider_credential_id=provider_credential_id,
        )
    workflow["name"] = workflow_name_value or workflow_name(source.id, workflow_operation_id)
    workflow["settings"]["timezone"] = str(
        source.configuration.get("timezone", "Asia/Ho_Chi_Minh")
    )
    bound = _bind_placeholders(workflow, (
        ("{{$env.SOURCE_ID}}", source_id),
        ("__SOURCE_ID__", source_id),
        ("__SOURCE_GENERATION__", str(source.generation)),
        ("__CONNECTOR_REVISION__", str(desired_revision)),
        ("__BACKEND_REVISION__", str(backend_revision)),
        ("__MCP_CONNECTION_ID__", str(source.configuration.get("connection_id", ""))),
    ))
    return {
        key: bound[key]
        for key in ("name", "nodes", "connections", "settings")
        if key in bound
    }


class N8nApi:
    """Access bounded n8n workflow management endpoints without ambient proxies."""

    def __init__(self, service_url: str, api_key: str, settings: Settings | None = None) -> None:
        """Store the n8n service root and API key header; ``settings`` enables DNS-pinned egress for owner-chosen hosts."""
        self._base_url = service_url.rstrip("/")
        self._headers = {"X-N8N-API-KEY": api_key}
        self._settings = settings

    @property
    def service_url(self) -> str:
        """Expose only the normalized actual dispatch endpoint for durable ownership fencing."""
        return n8n_service_identity(self._base_url)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """Return the HTTP client: pinned to approved addresses when settings are known, plain otherwise."""
        if self._settings is None:
            return httpx.AsyncClient(timeout=timeout, trust_env=False)
        from modules.settings.public import n8n_http_client
        return n8n_http_client(self._base_url, self._settings, timeout=timeout)

    async def find_workflows(self, name: str) -> list[dict[str, Any]]:
        """Find exact-name workflows through at most twenty paginated API pages."""
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(20):
            params: dict[str, str | int] = {"name": name, "limit": 250}
            if cursor is not None:
                params["cursor"] = cursor
            async with self._client(20) as client:
                response = await client.get(
                    f"{self._base_url}/api/v1/workflows",
                    headers=self._headers,
                    params=params,
                )
                response.raise_for_status()
            payload = response.json()
            workflows = payload.get("data", [])
            if not isinstance(workflows, list):
                raise ValueError("n8n workflow lookup returned an invalid list")
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
        async with self._client(20) as client:
            response = await client.get(
                f"{self._base_url}/api/v1/workflows/{workflow_id}",
                headers=self._headers,
            )
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("n8n workflow response is invalid")
        return payload

    async def create_workflow(self, workflow: dict[str, Any]) -> str:
        """Create a workflow and return its nonempty n8n identifier."""
        async with self._client(30) as client:
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
        async with self._client(30) as client:
            response = await client.put(
                f"{self._base_url}/api/v1/workflows/{workflow_id}",
                headers=self._headers,
                json=workflow,
            )
            response.raise_for_status()

    async def set_active(self, workflow_id: str, active: bool) -> None:
        """Activate or deactivate a workflow by its n8n identifier."""
        action = "activate" if active else "deactivate"
        async with self._client(20) as client:
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


_RSS_MAX_BYTES = 25 * 1024 * 1024
_RSS_MAX_RECORDS = 500
_RSS_MAX_PAGES = 10


def _local_tag(element: ElementTree.Element) -> str:
    """Return an XML element's lower-cased local tag name without its namespace."""
    return element.tag.rsplit("}", 1)[-1].lower()


def _feed_text(element: ElementTree.Element, names: set[str]) -> str:
    """Return descendant text for the first matching local XML tag name."""
    for child in element.iter():
        if _local_tag(child) in names:
            return "".join(child.itertext()).strip()
    return ""


def _parse_feed_time(raw_value: str) -> datetime | None:
    """Parse ISO or RFC feed time and normalize naive values as UTC; unparseable input yields None."""
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _read_rss_page_body(
    client: httpx.AsyncClient | None, fetch: Callable[[str], Awaitable[bytes]] | None,
    current_url: str, total_bytes: int,
) -> tuple[bytearray | None, int, str | None]:
    """Fetch one feed page body and return ``(body, total_bytes, redirect_url)``.

    With an injected ``fetch`` (native pinned transport) the body is returned as is. Otherwise the
    connection is pinned to the address validated here (closing the resolve-then-fetch DNS-rebinding
    window); a non-public or unresolvable host raises UnsafeDestination, never an unpinned request.
    A redirect returns ``(None, total_bytes, new_url)`` after validating the new URL. Total bytes
    across the whole walk are capped at 25 MiB (``ValueError``).
    """
    if fetch is not None:
        body = bytearray(await fetch(current_url))
        total_bytes += len(body)
        if total_bytes > _RSS_MAX_BYTES:
            raise ValueError("RSS pagination exceeded the 25 MiB limit")
        return body, total_bytes, None
    if client is None:
        raise RuntimeError("invariant violated: client must not be None")
    from modules.connectors.providers.rest import _pinned_request, resolve_public

    rss_parts = urlsplit(current_url)
    rss_port = rss_parts.port or (443 if rss_parts.scheme == "https" else 80)
    rss_addresses = await resolve_public(rss_parts.hostname or "", rss_port)
    send_url, send_headers, send_extensions = _pinned_request(
        current_url, rss_addresses[0],
        {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"},
    )
    async with client.stream("GET", send_url, headers=send_headers, extensions=send_extensions) as response:
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise ValueError("RSS redirect has no location")
            redirect_url = urljoin(current_url, location)
            await validate_public_url(redirect_url)
            return None, total_bytes, redirect_url
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            total_bytes += len(chunk)
            if total_bytes > _RSS_MAX_BYTES:
                raise ValueError("RSS pagination exceeded the 25 MiB limit")
            body.extend(chunk)
    return body, total_bytes, None


def _feed_record(
    item: ElementTree.Element, *, current_url: str, feed_url: str, floor: datetime | None,
) -> tuple[dict[str, object] | None, str | None]:
    """Normalize one feed item into ``(record, cursor_time)``; both are None below the overlap floor.

    ``cursor_time`` is set only for dated items so undated items never drag the cursor to "now".
    """
    from modules.connectors.registry import normalize

    identifier = _feed_text(item, {"guid", "id", "link"})
    title = _feed_text(item, {"title"})
    content = _feed_text(item, {"encoded", "content", "summary", "description"}) or title
    raw_date = _feed_text(item, {"published", "pubdate", "date"})
    raw_updated = _feed_text(item, {"updated"})
    observed_at = datetime.now(UTC)
    published_at = _parse_feed_time(raw_date)
    updated_at = _parse_feed_time(raw_updated)
    cursor_at = published_at or updated_at or observed_at
    link = next(
        (str(node.attrib.get("href")) for node in item.iter()
         if _local_tag(node) == "link" and node.attrib.get("href")),
        _feed_text(item, {"link"}),
    )
    canonical_url = urljoin(current_url, link) if link else None
    if floor is not None and cursor_at < floor:
        return None, None
    cursor_time = cursor_at.isoformat() if published_at or updated_at else None
    record = normalize(
        {
            "provider_id": identifier or feed_url,
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
    return record, cursor_time


def _append_page_records(
    root: ElementTree.Element, *, current_url: str, feed_url: str, floor: datetime | None,
    records: list[dict[str, object]], cursor_times: list[str], stats: dict[str, Any] | None,
) -> None:
    """Append a parsed page's records in order until the 500-record cap is reached.

    Mutates ``records``/``cursor_times``. Hitting the cap before the page's last item sets
    ``stats["truncated"]`` because no exact continuation exists for the unread remainder.
    """
    page_items = [node for node in root.iter() if _local_tag(node) in {"item", "entry"}]
    for position, item in enumerate(page_items):
        record, cursor_time = _feed_record(item, current_url=current_url, feed_url=feed_url, floor=floor)
        if record is not None:
            if cursor_time is not None:
                cursor_times.append(cursor_time)
            records.append(record)
        if len(records) == _RSS_MAX_RECORDS:
            if stats is not None and position < len(page_items) - 1:
                stats["truncated"] = True  # unread items remain on this page
            break


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
    if fetch is None:
        await validate_public_url(url)
    floor = overlap_floor(cursor, SKEW)  # small skew margin; already-stored items are dropped by dedup
    visited: set[str] = set()
    records: list[dict[str, object]] = []
    cursor_times: list[str] = []
    total_bytes = 0
    current_url: str | None = url
    async with contextlib.AsyncExitStack() as stack:
        client = None if fetch is not None else await stack.enter_async_context(
            httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False))
        for _ in range(_RSS_MAX_PAGES):
            if current_url is None or current_url in visited:
                break
            if fetch is None:
                await validate_public_url(current_url)
            visited.add(current_url)
            body, total_bytes, redirect_url = await _read_rss_page_body(client, fetch, current_url, total_bytes)
            if body is None:
                current_url = redirect_url
                continue
            # DOCTYPE/ENTITY refused; parse of up to 25 MiB runs in a thread so the loop is not blocked.
            root = await asyncio.to_thread(safe_xml_root, bytes(body), max_bytes=_RSS_MAX_BYTES)
            _append_page_records(
                root, current_url=current_url, feed_url=url, floor=floor,
                records=records, cursor_times=cursor_times, stats=stats,
            )
            next_link = next(
                (node.attrib.get("href") for node in root.iter() if _local_tag(node) == "link" and node.attrib.get("rel", "").lower() == "next"),
                None,
            )
            current_url = urljoin(current_url, next_link) if next_link else None
            if len(records) >= _RSS_MAX_RECORDS:
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
