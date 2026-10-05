"""Policy evaluation and permission enforcement for tool risk classifications and durable approval grants."""

from dataclasses import dataclass
from typing import Any

from core.tools.schemas import ToolApprovalGrant, ToolDefinition, ToolRisk


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of evaluating an execution attempt against the active tool policy."""

    allowed: bool
    requires_approval: bool
    reason: str


class ToolPolicy:
    """Permit read-only registered actions and fail closed on unowned effects."""

    def __init__(
        self,
        *,
        auto_approve_internal_write: bool = False,
        default_grant_expiry_seconds: int = 86400,
    ) -> None:
        """Initialize conservative compatibility settings; elevated effects remain unavailable.

        Args:
            auto_approve_internal_write: Retained setting for the later durable approval owner; not proof here.
            default_grant_expiry_seconds: Retained expiry policy for the later durable approval owner.
        """
        self.auto_approve_internal_write = auto_approve_internal_write
        self.default_grant_expiry_seconds = default_grant_expiry_seconds

    def evaluate(
        self,
        actor: str,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        grant: ToolApprovalGrant | None = None,
        *,
        trusted_approval: bool = False,
    ) -> PolicyDecision:
        """Allow ordinary read-only actions and reject confirmation/effects without the T3 verifier.

        Args:
            actor: Server-derived actor identity; grants no authority by itself.
            definition: Registered tool metadata and risk classification.
            arguments: Exact input parameters supplied to the tool.
            grant: Ignored untrusted grant-shaped input; it never proves owner authorization.
            trusted_approval: Internal registry result from the owning durable verifier, never request data.

        Returns:
            PolicyDecision indicating whether execution is allowed, whether approval is required, and rationale.
        """
        # The registry alone supplies this result after its async durable check; caller grants stay inert.
        if definition.risk == ToolRisk.READ_ONLY and not definition.confirmation_required:
            return PolicyDecision(True, False, "Read-only tool automatically permitted by policy.")
        if definition.risk in {ToolRisk.EXTERNAL_WRITE, ToolRisk.INTERNAL_WRITE} and trusted_approval:
            return PolicyDecision(True, False, "Exact action has a verified durable owner approval.")
        return PolicyDecision(False, True, "This effect has no matching verified durable approval.")
