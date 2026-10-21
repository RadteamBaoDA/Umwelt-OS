"""Remote erasure must preserve peer workflow IDs discovered after the first SQL read."""

from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from core.auth.erasure_contracts import AccountErasureScope
from modules.connectors import erasure
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorProvisioning,
    GithubOAuthGrant,
)


class _Session:
    def __init__(self, scalar_value: UUID | None, provisioning: object | None = None, *, peers=None, credentials=None, grant=None) -> None:
        self.scalar_value = scalar_value
        self.provisioning = provisioning
        self.peers = peers or []
        self.credentials = credentials or []
        self.grant = grant
        self.commit = AsyncMock()
        self.execute = AsyncMock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _query: object) -> UUID | None:
        return self.scalar_value

    async def get(self, model: object, _key: object) -> object | None:
        if model is ConnectorProvisioning:
            return self.provisioning
        return self.grant if model is GithubOAuthGrant else None

    async def scalars(self, _query: object) -> SimpleNamespace:
        rows = self.credentials if _query.column_descriptions[0]["name"] == "ConnectorManagedCredential" else self.peers
        return SimpleNamespace(all=lambda: rows)

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_discovered_workflow_with_peer_reference_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = UUID(int=11)
    peer_id = UUID(int=12)
    scope = AccountErasureScope(UUID(int=13), 7, UUID(int=14))
    provisioning = SimpleNamespace(
        workflow_id=None, old_workflow_id=None, workflow_name="owned",
        n8n_service_url="https://n8n.test",
        workflow_operation={"workflow_name": "owned", "step": {"request": {"name": "owned"}}},
    )
    sessions = iter((_Session(source_id, provisioning), _Session(peer_id, peers=["https://n8n.test"])))
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(erasure, "resolve_n8n", AsyncMock(return_value=SimpleNamespace(
        service_url="https://n8n.test", api_key="key",
    )))
    monkeypatch.setattr(erasure, "workflow_matches", lambda *_args: True)

    class _Api:
        def __init__(self, *_args: object) -> None:
            pass

        async def find_workflows(self, _name: str) -> list[dict[str, str]]:
            return [{"id": "shared-id"}]

        async def get_workflow(self, _identifier: str) -> dict[str, str]:
            return {"id": "shared-id"}

    monkeypatch.setattr(erasure, "N8nApi", _Api)
    monkeypatch.setattr(erasure, "n8n_http_client", lambda *_args, **_kwargs: pytest.fail(
        "peer workflow must never reach remote DELETE",
    ))
    monkeypatch.setattr(erasure, "N8nCredentials", lambda *_args: SimpleNamespace())
    done, cursor = await erasure.erase_account_remote_page(
        {"session_factory": lambda: next(sessions), "settings": SimpleNamespace()}, scope, {},
    )
    assert done is False
    assert cursor == {"after": str(source_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["dispatched", "unknown"])
async def test_inflight_creation_never_advances_erasure_even_when_lookup_would_be_empty(
    monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    provisioning = SimpleNamespace(
        workflow_id=None, old_workflow_id=None, workflow_name="owned",
        n8n_service_url="https://n8n.test",
        workflow_operation={"workflow_name": "owned", "step": {
            "kind": "create", "state": state, "request": {"name": "owned"},
            "dispatch_started_at": "2026-10-21T00:00:00Z",
        }},
    )
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(erasure, "N8nApi", lambda *_args: SimpleNamespace(find_workflows=lookup))
    with pytest.raises(RuntimeError, match="Unsettled n8n transport"):
        await erasure.erase_account_remote_page(
            {"session_factory": lambda: _Session(UUID(int=11), provisioning), "settings": SimpleNamespace()},
            AccountErasureScope(UUID(int=1), 7, UUID(int=2)), {},
        )
    lookup.assert_not_awaited()


@pytest.mark.parametrize("workflow", [False, True])
@pytest.mark.parametrize("outcome", ["known_success", "known_rejection", "not_sent", "unknown"])
def test_only_definitive_retained_transport_results_release_erasure(workflow: bool, outcome: str) -> None:
    step = {"kind": "create", "state": "unknown"}
    envelope = {"step": step} if workflow else dict(step)
    envelope["retained_effect_result"] = {
        "disposition": "retained_effect_access_revoked", "outcome": outcome,
        "remote_id": "late-created-id" if outcome == "known_success" else None,
    }
    if outcome == "unknown":
        with pytest.raises(RuntimeError, match="Unsettled n8n transport"):
            erasure._settled_remote_id(envelope, workflow=workflow)
    else:
        assert erasure._settled_remote_id(envelope, workflow=workflow) == (
            "late-created-id" if outcome == "known_success" else None
        )


@pytest.mark.asyncio
async def test_late_known_creation_is_deleted_by_its_exact_retained_result(monkeypatch: pytest.MonkeyPatch) -> None:
    source_id = UUID(int=11)
    provisioning = SimpleNamespace(
        workflow_id=None, old_workflow_id=None, workflow_name="owned",
        n8n_service_url="https://n8n.test",
        workflow_operation={"workflow_name": "owned", "step": {
            "kind": "create", "state": "dispatched", "request": {"name": "owned"},
        }, "retained_effect_result": {
            "disposition": "retained_effect_access_revoked", "outcome": "known_success",
            "remote_id": "late-created-id",
        }},
    )
    sessions = iter((_Session(source_id, provisioning), _Session(None)))
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(erasure, "resolve_n8n", AsyncMock(return_value=SimpleNamespace(
        service_url="https://n8n.test", api_key="key",
    )))
    monkeypatch.setattr(erasure, "N8nApi", lambda *_args: SimpleNamespace(find_workflows=AsyncMock(return_value=[])))
    deleted: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def delete(self, url, **_kwargs):
            deleted.append(url)
            return SimpleNamespace(status_code=404)

    monkeypatch.setattr(erasure, "n8n_http_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(erasure, "N8nCredentials", lambda *_args: SimpleNamespace())
    assert await erasure.erase_account_remote_page(
        {"session_factory": lambda: next(sessions), "settings": SimpleNamespace()},
        AccountErasureScope(UUID(int=1), 7, UUID(int=2)), {},
    ) == (False, {"after": str(source_id)})
    assert deleted == ["https://n8n.test/api/v1/workflows/late-created-id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", [True, False])
@pytest.mark.parametrize("peer_endpoint,deleted_count", [("https://other-n8n.test", 1), ("https://N8N.TEST:443/", 0)])
async def test_remote_id_peer_sharing_requires_original_endpoint_identity(monkeypatch, workflow, peer_endpoint, deleted_count):
    """The same provider ID on a different n8n host must not preserve departing data."""
    source_id = UUID(int=11)
    provisioning = SimpleNamespace(workflow_id="same-id" if workflow else None, old_workflow_id=None,
                                    workflow_name=None, workflow_operation=None, n8n_service_url="https://n8n.test")
    credential = SimpleNamespace(credential_id="same-id", operation_envelope=None, n8n_service_url="https://n8n.test")
    sessions = iter((_Session(source_id, provisioning, credentials=[] if workflow else [credential]),
                     _Session(None, peers=[peer_endpoint])))
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(erasure, "resolve_n8n", AsyncMock(return_value=SimpleNamespace(service_url="https://n8n.test", api_key="key")))
    deleted = AsyncMock(return_value=SimpleNamespace(status_code=404))
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        delete = deleted
    monkeypatch.setattr(erasure, "n8n_http_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(erasure, "N8nCredentials", lambda *_args: SimpleNamespace(delete=deleted))
    assert await erasure.erase_account_remote_page(
        {"session_factory": lambda: next(sessions), "settings": SimpleNamespace()},
        AccountErasureScope(UUID(int=1), 7, UUID(int=2)), {},
    ) == (False, {"after": str(source_id)})
    assert deleted.await_count == deleted_count


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [None, "https://previous-n8n.test"])
async def test_unknown_or_changed_origin_never_advances_remote_cleanup(monkeypatch, endpoint):
    """Mutable current settings cannot identify the original host of a legacy remote ID."""
    provisioning = SimpleNamespace(workflow_id="existing-id", old_workflow_id=None,
                                    workflow_name=None, workflow_operation=None, n8n_service_url=endpoint)
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(erasure, "resolve_n8n", AsyncMock(return_value=SimpleNamespace(service_url="https://n8n.test", api_key="key")))
    with pytest.raises(RuntimeError, match="endpoint lineage"):
        await erasure.erase_account_remote_page(
            {"session_factory": lambda: _Session(UUID(int=11), provisioning), "settings": SimpleNamespace()},
            AccountErasureScope(UUID(int=1), 7, UUID(int=2)), {},
        )


def test_unknown_peer_origin_cannot_silently_preserve_or_delete_remote_data():
    """A peer with unrecorded historical destination blocks completion pending reconciliation."""
    with pytest.raises(RuntimeError, match="Peer n8n endpoint lineage"):
        erasure._same_n8n_peer([None], "https://n8n.test")


@pytest.mark.parametrize("legacy,target,endpoint,allowed", [
    (None, False, "https://N8N.TEST:443/", True),
    (None, True, "https://n8n.test", False),
    ("https://n8n.test", True, "https://different.test", False),
    ("https://n8n.test/", True, "https://N8N.TEST:443", True),
])
def test_dispatch_origin_binding_never_reinterprets_existing_ids(legacy, target, endpoint, allowed):
    """New effects record their actual endpoint; existing effects cannot acquire guessed origins."""
    from modules.connectors.provisioning import _bind_n8n_service

    row = ConnectorManagedCredential(n8n_service_url=legacy)
    if allowed:
        _bind_n8n_service(row, endpoint, has_liability=target)
        assert row.n8n_service_url == "https://n8n.test"
    else:
        with pytest.raises(RuntimeError, match="endpoint lineage"):
            _bind_n8n_service(row, endpoint, has_liability=target)
        assert row.n8n_service_url == legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 204, 404, 500])
async def test_github_revoke_requires_explicit_provider_confirmation(monkeypatch, status):
    """Neither absent token errors nor unexpected successful HTTP codes prove grant revocation."""
    from modules.connectors.github import oauth

    original_client = httpx.AsyncClient
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, request=request)
    monkeypatch.setattr(oauth.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    settings = SimpleNamespace(github_app_client_id="client", github_app_client_secret=SimpleNamespace(get_secret_value=lambda: "secret"))
    if status == 204:
        await oauth.revoke_github_grant(settings, "token")
    else:
        with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
            await oauth.revoke_github_grant(settings, "token")
    assert len(requests) == 1 and requests[0].method == "DELETE"


@pytest.mark.asyncio
async def test_confirmed_github_revoke_is_committed_before_source_cursor_and_not_repeated(monkeypatch):
    """A worker retry after cursor loss uses the persisted app/user-wide confirmed outcome."""
    from modules.connectors.github import oauth

    source_id = UUID(int=11)
    grant = SimpleNamespace(source_id=source_id, encrypted_tokens="cipher", state="revoked",
                            error_code="provider_revoke_outcome_unknown", github_user_id="42", app_id="1",
                            operation_id=UUID(int=20), source_generation=1, configuration_revision=1)
    read_session = _Session(source_id, grant=grant)
    # The source lookup succeeds, then no peer grant shares this app/user.
    values = iter((source_id, None))
    read_session.scalar = AsyncMock(side_effect=lambda *_args: next(values))
    write_session = _Session(None)
    async def commit_known_result():
        grant.state, grant.encrypted_tokens, grant.error_code = "revoked", None, "provider_revoked"
    write_session.commit.side_effect = commit_known_result
    sessions = iter((read_session, write_session))
    monkeypatch.setattr(erasure, "authorize_account_erasure", AsyncMock())
    monkeypatch.setattr(erasure, "resolve_n8n", AsyncMock(return_value=SimpleNamespace(service_url="https://n8n.test", api_key="")))
    monkeypatch.setattr(oauth, "_open_token_cipher", lambda *_args: {"access_token": "token"})
    revoke = AsyncMock()
    monkeypatch.setattr(oauth, "revoke_github_grant", revoke)
    settings = SimpleNamespace(github_app_id="1", connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "key"))
    ctx = {"session_factory": lambda: next(sessions), "settings": settings}
    scope = AccountErasureScope(UUID(int=1), 7, UUID(int=2))
    assert await erasure.erase_account_remote_page(ctx, scope, {}) == (False, {"after": str(source_id)})
    write_session.commit.assert_awaited_once()
    query = write_session.execute.await_args.args[0]
    assert query.compile().params["error_code"] == "provider_revoked"
    ctx["session_factory"] = lambda: _Session(source_id, grant=grant)
    assert await erasure.erase_account_remote_page(ctx, scope, {}) == (False, {"after": str(source_id)})
    revoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["workflow", "credential", "activation"])
async def test_background_dispatch_uses_each_admitted_workspaces_url_and_key(monkeypatch, operation):
    """Background reconciliation cannot send tenant A's credential to tenant B's n8n host."""
    from modules.connectors import activation, provisioning, worker

    source_ids = [uuid4(), uuid4()]
    workspace_ids = [uuid4(), uuid4()]
    scopes = {source: SimpleNamespace(workspace_id=workspace) for source, workspace in zip(source_ids, workspace_ids, strict=True)}
    configurations = {workspace: SimpleNamespace(service_url=f"https://n8n-{index}.test", api_key=f"key-{index}", enabled=True)
                      for index, workspace in enumerate(workspace_ids)}
    envelope = {"kind": "enable", "step": {"state": "prepared", "kind": "create"}}
    async def admit(_session, source, *_args, **_kwargs):
        return scopes[source], object()
    async def resolve(_session, _settings, *, workspace_id):
        return configurations[workspace_id]
    monkeypatch.setattr(worker, "_admit_lineage", admit)
    monkeypatch.setattr("modules.settings.public.resolve_n8n", resolve)
    monkeypatch.setattr("modules.settings.public.module_is_enabled", AsyncMock(return_value=True))
    observed = []
    async def workflow_driver(_session, source, api, **_kwargs):
        observed.append((source, api.service_url, api._headers["X-N8N-API-KEY"]))
        return True
    async def activation_driver(_session, source, api, credentials, _key, **_kwargs):
        assert api.service_url == credentials.service_url
        observed.append((source, api.service_url, credentials._headers["X-N8N-API-KEY"]))
        return True
    class CredentialClient:
        def __init__(self, service_url, api_key):
            self.service_url, self.api_key = service_url, api_key
        async def delete(self, _identifier):
            observed.append((active_source, self.service_url, self.api_key))
    monkeypatch.setattr(provisioning, "drive_workflow_operation", workflow_driver)
    monkeypatch.setattr(activation, "drive_activation", activation_driver)
    monkeypatch.setattr(worker, "_settle_credential_delete", AsyncMock(return_value=True))
    monkeypatch.setattr(provisioning, "claim_credential_operation", AsyncMock(return_value={"target_id": "credential"}))
    if operation == "credential":
        monkeypatch.setattr(worker, "N8nCredentials", CredentialClient)
    settings = SimpleNamespace(multi_workspace_enabled=True, connector_credential_encryption_key=SimpleNamespace(get_secret_value=lambda: "unused"))
    for active_source in source_ids:
        session = MagicMock(scalar=AsyncMock(return_value=envelope), rollback=AsyncMock())
        if operation == "workflow":
            assert await worker._drive_workflow(session, settings, active_source, flag=True) == 1
        elif operation == "activation":
            assert await worker._resume_activation(session, active_source, settings)
        else:
            assert await worker._delete_credential(session, settings, active_source, "collector", uuid4(), multi_workspace_enabled=True)
    assert observed == [(source_ids[index], f"https://n8n-{index}.test", f"key-{index}") for index in range(2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,enabled,expected", [("enable", False, 0), ("deactivate", False, 1), ("enable", True, 1)])
async def test_background_workflow_kill_switch_is_scoped_and_preserves_cleanup(monkeypatch, kind, enabled, expected):
    """A workspace kill switch blocks create/update while its exact deactivation remains usable."""
    from modules.connectors import provisioning, worker

    workspace_id = uuid4()
    session = MagicMock(scalar=AsyncMock(return_value={"kind": kind, "step": {"state": "prepared", "kind": "create" if kind == "enable" else "deactivate"}}), rollback=AsyncMock())
    monkeypatch.setattr(worker, "_admit_lineage", AsyncMock(return_value=(SimpleNamespace(workspace_id=workspace_id), object())))
    resolve = AsyncMock(return_value=SimpleNamespace(service_url="https://n8n.test", api_key="key", enabled=enabled))
    monkeypatch.setattr("modules.settings.public.resolve_n8n", resolve)
    monkeypatch.setattr("modules.settings.public.module_is_enabled", AsyncMock(return_value=True))
    driver = AsyncMock(return_value=True)
    monkeypatch.setattr(provisioning, "drive_workflow_operation", driver)
    assert await worker._drive_workflow(session, SimpleNamespace(), uuid4(), flag=True) == expected
    assert resolve.await_args.kwargs == {"workspace_id": workspace_id}
    assert driver.await_count == expected
