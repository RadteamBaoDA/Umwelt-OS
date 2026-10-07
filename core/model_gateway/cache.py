import base64
import hashlib
import json
from uuid import UUID

from core.model_gateway.schemas import AIExecutionConfig
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext

_CAPABILITIES = "bbd:model-gateway:capability:"


def validate_capability_scope(config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Require matching typed cache subject and actual flag; caller still owns live admission."""
    if isinstance(scope, InternalJobScope):
        actor = scope.actor_user_id
    elif isinstance(scope, WorkspaceContext) and scope.role == "owner":
        actor = scope.user_id
    else:
        raise ValueError("Explicit owner scope is required")
    if type(multi_workspace_enabled) is not bool:
        raise ValueError("Explicit configured feature flag is required")
    if actor != 1 and not multi_workspace_enabled:
        raise ValueError("Multi-workspace execution is disabled")
    if (scope.workspace_id != config.workspace_id or actor != config.actor_user_id
            or scope.membership_revision != config.membership_revision):
        raise ValueError("Cache scope does not match execution configuration")


def _component(value: str) -> str:
    """Escape a key component so separators cannot change the Redis capability-key structure."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def capability_key(alias: str, model: str, version: str | None, capability: str, gateway_identity: str, *, workspace_id: UUID, actor_user_id: int) -> str:
    """Bind capability to workspace/actor and a nonsecret configuration digest.

    Gateway identity includes revisions, privacy and credential fingerprint. No bootstrap
    overload is accepted; a credential must never be passed as gateway_identity.
    """
    return f"{_namespace(workspace_id, actor_user_id, gateway_identity)}:{_component(alias)}:{_model_identity(model, version)}:{_component(capability)}"


def capability_alias_pattern(alias: str, *, workspace_id: UUID, actor_user_id: int, gateway_identity: str) -> str:
    """Return an alias invalidation pattern restricted to one execution identity."""
    return f"{_namespace(workspace_id, actor_user_id, gateway_identity)}:{_component(alias)}:*"


def capability_model_pattern(alias: str, model: str, version: str | None, *, workspace_id: UUID, actor_user_id: int, gateway_identity: str) -> str:
    """Return a model invalidation pattern restricted to one execution identity."""
    return f"{_namespace(workspace_id, actor_user_id, gateway_identity)}:{_component(alias)}:{_model_identity(model, version)}:*"


def _namespace(workspace_id: UUID, actor_user_id: int, gateway_identity: str) -> str:
    """Reject missing identity; key material is UUID, actor and SHA256 digest only."""
    if not isinstance(workspace_id, UUID) or type(actor_user_id) is not int or actor_user_id <= 0:
        raise ValueError("Explicit workspace and positive actor are required")
    if len(gateway_identity) != 64 or any(char not in "0123456789abcdef" for char in gateway_identity):
        raise ValueError("Gateway identity must be a nonsecret SHA256 digest")
    return f"{_CAPABILITIES}w:{workspace_id}:u:{actor_user_id}:g:{gateway_identity}"


def _model_identity(model: str, version: str | None) -> str:
    """Hash the model/version tuple into a stable cache-key component."""
    identity = json.dumps((model, version), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
