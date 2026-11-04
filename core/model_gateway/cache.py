from core.model_gateway.schemas import AIExecutionConfig
from core.workspaces.schemas import InternalJobScope, Scope, WorkspaceContext


def validate_capability_scope(config: AIExecutionConfig, scope: Scope, multi_workspace_enabled: bool) -> None:
    """Require matching typed cache subject and actual flag; caller still owns live admission.

    Capability evidence now lives in Postgres (``model_test_results``); this scope check still gates reads and
    probe writes so evidence is only ever produced for the configured workspace/actor.
    """
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
