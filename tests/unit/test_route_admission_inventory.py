"""Route admission inventory for the W4-routes flip: operator routes vs account/workspace routes."""

from fastapi import APIRouter
from fastapi.routing import APIRoute

from modules.connectors.github.routes import router as github_router
from modules.connectors.provisioning_routes import router as provisioning_router
from modules.connectors.routes import operator_router as connectors_operator_router
from modules.connectors.routes import router as connectors_router
from modules.goals.routes import router as goals_router
from modules.ingestion.routes import documents_router
from modules.ingestion.routes import router as ingestion_router
from modules.knowledge.entities.routes import router as entities_router
from modules.knowledge.observations.routes import router as observations_router
from modules.knowledge.relationships.routes import router as relationships_router
from modules.knowledge.temporal.routes import router as temporal_router
from modules.observability.operations_routes import router as operations_router
from modules.observability.routes import router as observability_router
from modules.settings.onboarding_routes import router as onboarding_router
from modules.sources.routes import router as sources_router
from modules.tasks.routes import router as tasks_router
from modules.timeline.routes import router as timeline_router

OPERATOR = {"require_owner", "require_owner_write"}
SCOPED = {
    "require_account", "require_account_write", "require_workspace_read", "require_workspace_write",
    "require_default_workspace_read", "require_default_workspace_write",
}
# Routes that deliberately keep the bootstrap-operator dependency.
OPERATOR_ROUTES = {
    ("GET", "/api/v1/connectors/github/webhook-status"),
    ("PUT", "/api/v1/operator/connectors/{workspace_id}/{source_id}/terms-review"),
}
# Capability-token ingress: authenticates by bearer/signature inside the handler, not by session.
CAPABILITY_ROUTES = {
    ("POST", "/api/v1/ingestion/batches"),
    ("POST", "/api/v1/connectors/github/webhook"),
    ("POST", "/api/v1/connectors/sources/{source_id}/mcp-collect"),
    ("POST", "/api/v1/connectors/sources/{source_id}/crawl"),
    ("POST", "/api/v1/connectors/sources/{source_id}/collection-admission"),
    ("POST", "/api/v1/connectors/sources/{source_id}/no-changes"),
    ("POST", "/api/v1/connectors/sources/{source_id}/sync"),
    ("GET", "/api/v1/connectors/sources/{source_id}/rss"),
    ("POST", "/api/v1/connectors/sources/{source_id}/validate"),
    ("POST", "/api/v1/connectors/sources/{source_id}/provider-fetch"),
}
OPERATOR_ROUTERS = (operations_router, observability_router)
FLIPPED_ROUTERS = (
    tasks_router, goals_router, timeline_router, entities_router, relationships_router, temporal_router,
    observations_router, sources_router, ingestion_router, documents_router, connectors_router,
    provisioning_router, github_router, onboarding_router,
)


def _calls(dependant: object, acc: set[str]) -> set[str]:
    for sub in dependant.dependencies:  # type: ignore[attr-defined]
        if sub.call is not None:
            acc.add(sub.call.__name__)
        _calls(sub, acc)
    return acc


def _routes(router: APIRouter) -> list[tuple[APIRoute, set[str]]]:
    return [(r, _calls(r.dependant, set())) for r in router.routes if isinstance(r, APIRoute)]


def test_flipped_routes_are_scoped_or_listed_operator() -> None:
    seen_operator: set[tuple[str, str]] = set()
    for router in (*FLIPPED_ROUTERS, connectors_operator_router):
        for route, calls in _routes(router):
            key = (min(route.methods), route.path)
            if calls & OPERATOR:
                assert key in OPERATOR_ROUTES, f"unlisted operator route {key}"
                seen_operator.add(key)
            elif key not in CAPABILITY_ROUTES:
                assert calls & SCOPED, f"route with neither operator nor scoped dependency {key}"
    assert seen_operator == OPERATOR_ROUTES


def test_observability_routes_stay_operator_only() -> None:
    for router in OPERATOR_ROUTERS:
        rows = _routes(router)
        assert rows
        for route, calls in rows:
            assert calls & OPERATOR and not calls & SCOPED, route.path
