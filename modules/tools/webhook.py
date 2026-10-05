"""Deployment-pinned, single-attempt webhook action registration for durable approval runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
import time
from typing import Any

from core.config import Settings
from core.mcp_endpoint import normalize_mcp_url
from core.tools import ToolDefinition, ToolRegistry, ToolResult, ToolRisk
from modules.tools.mcp_transport import McpOperationNetworkBudget, McpPinnedHttpTransport


@dataclass(frozen=True)
class WebhookProfile:
    """Expose one immutable deployment alias, exact HTTPS endpoint origin, and revision identity."""

    alias: str
    endpoint: str
    origin: tuple[str, str, int]
    cidrs: tuple[str, ...]
    revision: str


def load_webhook_profiles(settings: Settings) -> dict[str, WebhookProfile]:
    """Parse bounded secret-free deployment aliases and reject unsafe or mutable endpoint shapes.

    The profile manifest is deployment-owned, never accepted from tool arguments. Its digest binds
    approvals to the URL, normalized origin, CIDR exceptions, and enabled state.
    """
    raw = settings.webhook_profiles_json
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("WEBHOOK_PROFILES is invalid JSON") from exc
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("WEBHOOK_PROFILES must contain at most 32 aliases")
    private_networks = tuple(ipaddress.ip_network(item) for item in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "127.0.0.0/8", "::1/128",
    ))
    result: dict[str, WebhookProfile] = {}
    for alias, item in value.items():
        if (not isinstance(alias, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", alias) is None
                or not isinstance(item, dict) or set(item) != {"url", "cidrs", "enabled"}
                or not isinstance(item["url"], str) or type(item["enabled"]) is not bool
                or not isinstance(item["cidrs"], list) or len(item["cidrs"]) > 16
                or any(not isinstance(cidr, str) for cidr in item["cidrs"])):
            raise ValueError("WEBHOOK_PROFILES contains an invalid alias")
        scheme, host, port, _path = normalize_mcp_url(item["url"])
        if scheme != "https" or not item["enabled"]:
            continue
        cidrs = []
        for value_cidr in item["cidrs"]:
            network = ipaddress.ip_network(value_cidr, strict=False)
            if (network.prefixlen == 0
                    or isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None
                    or not any(network.version == allowed.version and network.subnet_of(allowed) for allowed in private_networks)):
                raise ValueError("Webhook CIDRs must be bounded private or loopback networks")
            cidrs.append(network.with_prefixlen)
        origin = (scheme, host, port)
        identity = json.dumps({"alias": alias, "url": item["url"], "origin": origin, "cidrs": sorted(set(cidrs)), "enabled": True}, sort_keys=True, separators=(",", ":"))
        result[alias] = WebhookProfile(alias, item["url"], origin, tuple(sorted(set(cidrs))), hashlib.sha256(identity.encode()).hexdigest())
    return result


def register_webhook_tool(registry: ToolRegistry, settings: Settings) -> None:
    """Register one fixed-profile webhook tool only when deployment supplied enabled aliases."""
    profiles = load_webhook_profiles(settings)
    if not profiles:
        return
    registry.register_tool(ToolDefinition(
        name="webhook.send", version="1.0.0",
        description="Send one owner-approved JSON payload to a configured HTTPS webhook profile.",
        input_schema={
            "type": "object", "required": ["profile", "payload"], "additionalProperties": False,
            "properties": {
                "profile": {"type": "string", "enum": sorted(profiles)},
                "payload": {"type": "object", "maxProperties": 128},
            },
        },
        output_schema={
            "type": "object", "required": ["accepted", "status_code", "result_reference"],
            "properties": {
                "accepted": {"type": "boolean"}, "status_code": {"type": "integer", "minimum": 200, "maximum": 299},
                "result_reference": {"type": "string", "minLength": 1, "maxLength": 256},
            }, "additionalProperties": False,
        },
        risk=ToolRisk.EXTERNAL_WRITE, confirmation_required=True, timeout_seconds=30,
        max_arguments_bytes=64_000, max_result_bytes=8_192, permissions=("webhook.send",), module="tools",
    ), _send_webhook)


async def _send_webhook(arguments: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Send at most one credential-free POST after the approval owner commits the no-replay slot.

    A deterministic key is correlation only; generic receivers do not guarantee idempotency. Once
    the transport's pre-send fence marks the attempt in flight, ambiguous outcomes become review-only.
    """
    from httpx2 import AsyncClient, Timeout
    from modules.agents.approvals import mark_effect_outcome

    profiles = load_webhook_profiles(context["settings"])
    alias = arguments.get("profile")
    profile = profiles.get(alias) if isinstance(alias, str) else None
    action_id = context.get("action_id")
    run_id = context.get("run_id")
    before_send = context.get("before_webhook_send")
    if not isinstance(action_id, str) or not isinstance(run_id, str):
        return ToolResult(success=False, error="Approved webhook action is unavailable", error_code="forbidden")
    if profile is None or not callable(before_send):
        await mark_effect_outcome(context["session_factory"], action_id, "failed", None)
        return ToolResult(success=False, error="Approved webhook action is unavailable", error_code="forbidden")
    started = False

    async def fenced_send() -> bool:
        """Revalidate current run/action/profile after DNS and immediately before the one socket send."""
        nonlocal started
        allowed = await before_send(profile, action_id)
        started = allowed
        return allowed

    budget = McpOperationNetworkBudget(
        deadline=time.monotonic() + 30, max_requests=1, max_request_bytes=64_000,
        max_response_bytes=8_192, max_inflight_requests=1, before_request=fenced_send,
    )
    transport = McpPinnedHttpTransport(
        profile.endpoint, budget,
        approved_destination_cidrs={profile.origin: profile.cidrs},
    )
    result_reference = f"action:{action_id}"
    try:
        async with AsyncClient(transport=transport, follow_redirects=False, timeout=Timeout(30)) as client:
            response = await client.post(
                profile.endpoint, json=arguments["payload"],
                headers={"Idempotency-Key": action_id},
            )
            await response.aread()
            if 200 <= response.status_code < 300:
                await mark_effect_outcome(
                    context["session_factory"], action_id, "succeeded", result_reference,
                    result_status_code=response.status_code,
                )
                return ToolResult(success=True, data={
                    "accepted": True, "status_code": response.status_code,
                    "result_reference": result_reference,
                })
            await mark_effect_outcome(
                context["session_factory"], action_id, "requires_review", result_reference,
                result_status_code=response.status_code,
            )
            return ToolResult(success=False, error="Webhook outcome requires review", error_code="execution_failed")
    except asyncio.CancelledError:
        # Registry/harness deadline or run cancel: a started send has an unknown outcome, so record
        # it review-only (shielded so the cancellation cannot abort the write) and keep unwinding.
        await asyncio.shield(mark_effect_outcome(
            context["session_factory"], action_id,
            "requires_review" if started else "failed", result_reference if started else None,
        ))
        raise
    except Exception:
        await mark_effect_outcome(
            context["session_factory"], action_id,
            "requires_review" if started else "failed", result_reference if started else None,
        )
        return ToolResult(success=False, error="Webhook delivery failed", error_code="execution_failed")
    finally:
        await transport.aclose()


async def send_once(
    settings: Settings, alias: str, payload: dict[str, Any], *, idempotency_key: str,
    headers: dict[str, str], before_send: Any,
) -> str:
    """Send at most one credential-free POST for a caller that owns its own no-replay ledger.

    Used by automation actions, whose approval and effect state live in the automation run ledger
    (P07 approvals are bound to agent runs). The same deployment alias allowlist, pinned transport,
    private-CIDR rules, single request, no redirects and size budgets as ``webhook.send`` apply.
    ``before_send`` is awaited after DNS and immediately before the socket write; returning False
    aborts with nothing sent. Returns ``"succeeded"`` (2xx), ``"unsent"`` (rejected or failed before
    the write, safe to treat as not delivered) or ``"ambiguous"`` (may have reached the receiver:
    non-2xx, timeout or cancellation after the write began; the caller must never replay it).
    """
    from httpx2 import AsyncClient, Timeout

    profile = load_webhook_profiles(settings).get(alias)
    if profile is None:
        return "unsent"
    started = False

    async def fenced_send() -> bool:
        """Run the caller fence after DNS and record whether the write was allowed to begin."""
        nonlocal started
        started = bool(await before_send())
        return started

    budget = McpOperationNetworkBudget(
        deadline=time.monotonic() + 30, max_requests=1, max_request_bytes=64_000,
        max_response_bytes=8_192, max_inflight_requests=1, before_request=fenced_send,
    )
    transport = McpPinnedHttpTransport(profile.endpoint, budget, approved_destination_cidrs={profile.origin: profile.cidrs})
    try:
        async with AsyncClient(transport=transport, follow_redirects=False, timeout=Timeout(30)) as client:
            response = await client.post(
                profile.endpoint, json=payload, headers={**headers, "Idempotency-Key": idempotency_key})
            await response.aread()
            return "succeeded" if 200 <= response.status_code < 300 else "ambiguous"
    except Exception:
        # CancelledError is not caught here: the caller's committed in-flight row stays review-only.
        return "ambiguous" if started else "unsent"
    finally:
        await transport.aclose()
