"""Characterization matrix for ``accept_native_collection`` (provider x lease/replay/recheck/record validation).

The function is exercised against a scripted session and spies for every collaborator so the tests pin
(1) the result or raised ``HTTPException`` status/detail, and (2) the exact lock/call ORDER documented by the
contract: credential -> collector -> grant -> state -> receipt, replay lookup after the grant fences but before
the token/cursor recheck, and replay-head publication (``commit_with_replay``) last.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.errors import DomainError
from core.workspaces.schemas import AccessFence, InternalJobScope
from modules.connectors import public as connectors
from modules.ingestion import public as ingestion
from modules.ingestion.schemas import (
    ConnectorCollectionLease,
    IngestionRecord,
    NativeCollectionBatch,
    TelegramDeliveryProof,
    TelegramProbeClassification,
    TelegramRawDelivery,
)
from modules.knowledge.documents.schemas import ProviderRecordMetadata
from modules.sources import public as sources

WORKSPACE = uuid4()
SOURCE_ID = uuid4()
NOW = datetime.now(UTC)
SCOPE = InternalJobScope(workspace_id=WORKSPACE, actor_user_id=7, membership_revision=3)
ACCESS = AccessFence(WORKSPACE, 7, 3, 5)
LEASE_TOKEN = uuid4()
LEASE_EXPIRY = NOW + timedelta(minutes=5)
NATIVE_OP = uuid4()
WORLD_OP = uuid4()
CHAT = "-1001"
BOT = "123"

# Modules that may own the implementation (the facade today, an internal submodule after the split).
_CANDIDATES = (
    "modules.ingestion.public", "modules.ingestion._native_collection", "modules.ingestion._native_verifiers",
    "modules.ingestion._native_records",
    "modules.ingestion._collection", "modules.ingestion._credentials",
)


def _owner_modules() -> list[Any]:
    """Return every importable module that may hold the implementation's collaborator names."""
    found = []
    for name in _CANDIDATES:
        try:
            found.append(importlib.import_module(name))
        except ImportError:
            continue
    return found


class FakeSession:
    """Scripted AsyncSession: entity-keyed ``scalar`` results, recorded ``add``/``flush`` calls."""

    def __init__(self, calls: list[str], *, state: Any, replay: str | None, run: Any, grant: Any, credential_revision: int) -> None:
        self.calls = calls
        self.state = state
        self.replay = replay  # None: no batch; "same": hash matches; "other": hash conflicts
        self.run = run
        self.grant = grant
        self.credential_revision = credential_revision
        self.added: list[Any] = []

    async def scalar(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
        """Answer by the statement's first entity; record the lookup in call order."""
        entity = stmt.column_descriptions[0]["entity"].__name__
        self.calls.append(f"scalar:{entity}")
        if entity == "SourceIngestionState":
            return self.state
        if entity == "IngestionBatch":
            if self.replay is None:
                return None
            params = stmt.compile().params
            batch_key = next(v for v in params.values() if isinstance(v, str) and v.startswith("native:"))
            digest = batch_key.rsplit(":", 1)[1] if self.replay == "same" else "0" * 64
            return SimpleNamespace(id=uuid4(), payload_hash=digest)
        if entity == "IngestionRun":
            return self.run
        if entity == "ConnectorOAuthGrant":
            return self.grant
        if entity == "ConnectorProvisioning":
            return self.credential_revision
        raise AssertionError(entity)

    def add(self, obj: Any) -> None:
        """Record ORM inserts and assign the id the flush would."""
        self.calls.append(f"add:{type(obj).__name__}")
        if getattr(obj, "id", None) is None and hasattr(obj, "id"):
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        """Record a flush boundary (the function must never commit)."""
        self.calls.append("flush")

    async def commit(self) -> None:  # pragma: no cover - must never be reached
        raise AssertionError("accept_native_collection must flush only; the caller owns the transaction")


@dataclass
class Case:
    """One scenario: provider, scripted knobs and the expected outcome."""

    provider: str
    kwargs: dict[str, Any] = field(default_factory=dict)  # accept_native_collection keyword overrides
    payload: dict[str, Any] = field(default_factory=dict)  # NativeCollectionBatch field overrides
    lease: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    replay: str | None = None
    records: str = "none"  # none | provider
    knobs: dict[str, Any] = field(default_factory=dict)


def _metadata(provider: str, record_id: str, version: str, *, coverage: str = "returned_snapshot") -> dict[str, Any]:
    """Build a valid ``provider_record`` block for ``provider`` (Telegram gets its typed detail)."""
    body: dict[str, Any] = {
        "provider": provider, "identity": record_id, "provider_version": version,
        "timestamp_basis": "collection", "coverage": coverage, "content_truncated": False,
    }
    if provider == "telegram":
        body["telegram"] = {
            "bot_id": BOT, "channel_id": CHAT, "message_id": "9", "epoch": 1, "update_id": 11,
            "edited_received": False, "published_at": NOW.isoformat(),
        }
    return ProviderRecordMetadata.model_validate(body).model_dump(mode="json")


def _record(provider: str, *, bad_identity: bool = False, bad_coverage: bool = False) -> IngestionRecord:
    """Build one record whose metadata matches (or deliberately breaks) the owner contract."""
    record_id = "item-1"
    meta_id = "other" if bad_identity else record_id
    coverage = "truncated" if bad_coverage else "returned_snapshot"
    return IngestionRecord(
        provider_id=record_id, content="body", observed_at=NOW, version="v1",
        metadata={"provider_record": _metadata(provider, meta_id, "v1", coverage=coverage)},
    )


def _raw(update_id: int = 11) -> TelegramRawDelivery:
    """Build a raw Telegram delivery with a correct canonical digest."""
    update = {"update_id": update_id, "message": {"text": "hi"}}
    encoded = json.dumps(update, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return TelegramRawDelivery(update_id=update_id, raw_update_sha256=hashlib.sha256(encoded).hexdigest(), update=update)


@pytest.fixture(autouse=True)
def _tuple_tolerant_json_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``GitHubSegmentProof`` validate its tuple ``raw_items`` (the same shim the adapter tests use)."""
    from modules.ingestion import github_proof as github_schemas

    original = github_schemas._bounded_json_counts
    monkeypatch.setattr(
        github_schemas, "_bounded_json_counts",
        lambda value: original(list(value) if isinstance(value, tuple) else value),
    )


def _github_proof(**overrides: Any) -> Any:
    """Build a minimal valid GitHub segment proof (found target, no hint claim)."""
    from modules.connectors.github.schemas import GitHubBindingFence, GitHubSegmentProof

    items: tuple[dict[str, Any], ...] = ()
    raw_sha = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    fence = GitHubBindingFence(
        source_id=SOURCE_ID, source_generation=1, connector_revision=2, grant_operation_id=uuid4(), token_revision=1,
        repository_id="10", installation_id="20", app_id="30", binding_revision=1, resource_scope=("issue",),
        history_days=30, scope_sha256="a" * 64,
    )
    data: dict[str, Any] = {
        "fence": fence, "resource": "issue", "page": 1, "sweep_revision": 1, "scan_floor": NOW - timedelta(days=1),
        "scan_upper": NOW, "raw_items": items, "raw_sha256": raw_sha, "transport_bytes": 2, "next_page": None,
        "has_next": False, "collected_at": NOW,
    }
    data.update(overrides)
    return GitHubSegmentProof(**data)


def _hint(intent: str, locator_kind: str) -> Any:
    """Build a hint-claim proof for the visibility-fence matrix."""
    from modules.connectors.github.schemas import GitHubHintClaimProof

    return GitHubHintClaimProof(
        source_id=SOURCE_ID, source_generation=1, connector_revision=2, repository_id="10", installation_id="20",
        binding_revision=1, hint_id=uuid4(), dirty_revision=1, resource="issue", locator_kind=locator_kind,
        locator="1", intent=intent, lease_token=uuid4(), expires_at=NOW + timedelta(minutes=5),
    )


class Harness:
    """Wire spies for every collaborator and run one case through ``accept_native_collection``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, case: Case) -> None:
        self.calls: list[str] = []
        self.case = case
        self.mp = monkeypatch
        self.source = SimpleNamespace(
            id=SOURCE_ID, provider=case.provider, status="active", local_only=False, generation=1,
            workspace_id=WORKSPACE, configuration={"telegram_chat_ids": [CHAT]},
        )
        self.knobs = {
            "collection_fence": True, "revision_ok": True, "credential": "ready", "world_ok": True,
            "github_fence": "match", "segment": "match", "hint_ack": True, "paused": "ok", "classification": "ok",
            "telegram_validate": "ok", "seen": set(), "grant": "ok", **case.knobs,
        }
        self.state = SimpleNamespace(
            source_id=SOURCE_ID, cursor=None, lease_run_id=None, collection_lease_token=LEASE_TOKEN,
            lease_expires_at=LEASE_EXPIRY, continuation_state=None, etag=None, last_modified=None,
            validators_revision=None,
        )
        for key, value in case.state.items():
            setattr(self.state, key, value)
        self.lease = ConnectorCollectionLease(**{
            "workspace_id": WORKSPACE, "actor_user_id": 7, "membership_revision": 3, "source_id": SOURCE_ID,
            "source_generation": 1, "configuration_revision": 5, "connector_revision": 2, "token": LEASE_TOKEN,
            "cursor_before": None, "expires_at": LEASE_EXPIRY, **case.lease,
        })
        self.github_fence = None
        self.payload = self._payload()
        grant = SimpleNamespace(
            state="ready", encrypted_tokens=b"x", workspace_id=WORKSPACE, owner_id=7, provider=case.provider,
            source_generation=1, configuration_revision=2, credential_revision=4,
            operation_id=self.payload.google_operation_id or self.payload.reddit_operation_id, token_revision=1,
        )
        if self.knobs["grant"] == "stale":
            grant.state = "revoked"
        self.session = FakeSession(
            self.calls, state=self.state, replay=case.replay, run=SimpleNamespace(id=uuid4()), grant=grant,
            credential_revision=4,
        )
        self._install()

    def _payload(self) -> NativeCollectionBatch:
        """Assemble the provider-appropriate payload for the scenario."""
        case = self.case
        base: dict[str, Any] = {
            "source_id": SOURCE_ID, "source_generation": 1, "connector_revision": 2, "lease_token": LEASE_TOKEN,
            "cursor_before": None, "cursor_after": "c1", "records": [], "telegram_deliveries": (),
            "telegram_raw_deliveries": (), "coverage": "returned_snapshot", "collected_at": NOW,
        }
        if case.provider == "telegram":
            raw = _raw()
            proof = TelegramDeliveryProof(bot_id=BOT, epoch=1, update_id=11, raw_update_sha256=raw.raw_update_sha256)
            base.update(telegram_raw_deliveries=(raw,), telegram_deliveries=(proof,), cursor_after=None)
        if case.provider.startswith("google"):
            base.update(google_operation_id=uuid4(), google_token_revision=1)
        if case.provider == "reddit":
            base.update(reddit_operation_id=uuid4(), reddit_token_revision=1)
        if case.provider == "github":
            self.github_proof = _github_proof(**case.knobs.get("proof_overrides", {}))
            base.update(github_segment=self.github_proof)
            self.github_fence = self.github_proof.fence
        if case.provider in {"notion", "slack", "x"}:
            base.update(provider_credential_revision=9)
        if case.records == "provider":
            base["records"] = [_record(case.provider, **self.knobs.get("record_flags", {}))]
        base.update(case.payload)
        return NativeCollectionBatch(**base)

    def default_kwargs(self) -> dict[str, Any]:
        """Keyword arguments matching the provider's capture contract."""
        provider = self.case.provider
        kwargs: dict[str, Any] = {
            "collector_token": "tok", "lease": self.lease, "scope": SCOPE, "multi_workspace_enabled": False,
            "expected_native_operation_id": NATIVE_OP if provider == "telegram" else None,
            "expected_world_credential_operation_id": WORLD_OP if provider == "alpha_vantage" else None,
            "provider_credential_revision": 9 if provider in {"notion", "slack", "x"} else None,
        }
        kwargs.update(self.case.kwargs)
        return kwargs

    def _spy(self, label: str, result: Any = None, *, owner: Any = None, name: str | None = None, sync: bool = False) -> None:
        """Replace ``name`` on every owner module that defines it with a recording stub."""
        target_name = name or label
        owners = [owner] if owner is not None else [m for m in _owner_modules() if hasattr(m, target_name)]
        assert owners, target_name

        async def stub(*_a: Any, **_k: Any) -> Any:
            self.calls.append(label)
            return result(*_a, **_k) if callable(result) else result

        def sync_stub(*_a: Any, **_k: Any) -> Any:
            self.calls.append(label)
            return result(*_a, **_k) if callable(result) else result

        for module in owners:
            self.mp.setattr(module, target_name, sync_stub if sync else stub)

    def _install(self) -> None:
        """Install the collaborator spies; each appends its label so ORDER is observable."""
        k = self.knobs

        async def lock_projection(*_a: Any, **_kw: Any) -> Any:
            self.calls.append("lock_source_projection")
            return (self.source if k.get("source") != "missing" else None), SimpleNamespace(), ACCESS

        for module in _owner_modules():
            if hasattr(module, "_lock_source_projection"):
                self.mp.setattr(module, "_lock_source_projection", lock_projection)
        for module in _owner_modules():
            if hasattr(module, "lock_source_credentials_in_uow"):
                async def lock_credentials(*_a: Any, **_kw: Any) -> None:
                    self.calls.append("lock_source_credentials")
                self.mp.setattr(module, "lock_source_credentials_in_uow", lock_credentials)
        self._spy("authorize_collection", name="_authorize_collection")
        self._spy("settle_request", name="_settle_request")
        self._spy("commit_with_replay")
        self._spy("publish_event")
        self._spy("schedule_normalization", None)
        self._spy("seen_record_keys", lambda *a, **kw: k["seen"])
        self._spy("collection_fence", lambda *a, **kw: k["collection_fence"], owner=connectors, name="require_collection_fence")
        self._spy("provider_revision", lambda *a, **kw: k["revision_ok"], owner=connectors,
                  name="validate_provider_credential_revision_in_uow")
        self._spy("world_operation", lambda *a, **kw: k["world_ok"], owner=connectors,
                  name="validate_world_credential_operation_in_uow")
        self._spy("native_credential", self._credential, owner=connectors, name="get_native_credential_snapshot")
        self._spy("github_binding_fence", lambda *a, **kw: self._github_fence(), owner=connectors,
                  name="lock_github_binding_fence_in_uow")
        self._spy("github_segment", self._segment, owner=connectors, name="validate_github_collection_segment_in_uow")
        self._spy("github_hints_lock", owner=connectors, name="lock_github_hints_for_visibility_pause_in_uow")
        self._spy("github_ack_in_uow", lambda *a, **kw: k["hint_ack"], owner=connectors, name="acknowledge_github_hint_in_uow")
        self._spy("github_ack", lambda *a, **kw: k["hint_ack"], owner=connectors, name="acknowledge_github_hint")
        self._spy("prepare_pause", owner=sources, name="prepare_source_pause_for_connector_in_uow")
        self._spy("pause_source", lambda *a, **kw: SimpleNamespace(id=SOURCE_ID, generation=2, status="paused")
                  if k["paused"] == "ok" else None, owner=sources, name="pause_source_for_connector_in_uow")
        self._spy("record_collection_result", owner=sources, name="record_collection_result_in_uow")
        self._spy("classify_telegram_probe", self._classify, name="classify_telegram_probe", sync=True)
        self._spy("validate_telegram_record", self._validate_telegram, name="validate_telegram_record_delivery", sync=True)

    def _credential(self, *_a: Any, **_k: Any) -> Any:
        """Telegram native credential snapshot scripted by the ``credential`` knob."""
        mode = self.knobs["credential"]
        if mode == "missing":
            return None
        return SimpleNamespace(
            source_generation=1, configuration_revision=2,
            operation_id=NATIVE_OP if mode != "op-changed" else uuid4(),
            access_fence=ACCESS if mode != "fence-changed" else AccessFence(WORKSPACE, 7, 3, 99),
            workspace_id=WORKSPACE, source_id=SOURCE_ID, state="ready" if mode != "not-ready" else "pending",
            verified_bot_id=BOT, encrypted_token=b"t", validated_at=NOW,
        )

    def _github_fence(self) -> Any:
        """Locked GitHub binding fence: matching, absent or different."""
        mode = self.knobs["github_fence"]
        if mode == "missing":
            return None
        if mode == "different":
            return self.github_proof.fence.model_copy(update={"binding_revision": 99})
        return self.github_fence

    def _segment(self, *_a: Any, **_k: Any) -> Any:
        """Validated segment: echoes the supplied records/cursor/coverage unless a knob breaks one."""
        mode = self.knobs["segment"]
        records = [r for r in self.payload.records]
        cursor_after = self.payload.cursor_after if mode != "cursor" else "other"
        coverage = self.payload.coverage if mode != "coverage" else "truncated"
        if mode == "records":
            records = []
        if mode == "moves-cursor":
            cursor_after = "moved"
        return SimpleNamespace(records=records, cursor_after=cursor_after, coverage=coverage)

    def _classify(self, *_a: Any, **_k: Any) -> TelegramProbeClassification:
        """Telegram probe classification scripted by the ``classification`` knob."""
        mode = self.knobs["classification"]
        proofs = tuple(self.payload.telegram_deliveries)
        if mode == "conflict":
            return TelegramProbeClassification(delivery_proofs=(), replay_update_ids=(), cursor_after=None,
                                               conflict_code="telegram_stream_conflict")
        if mode == "stale-proof":
            proofs = ()
        replay = (11,) if mode == "replay" else ()
        return TelegramProbeClassification(delivery_proofs=proofs, replay_update_ids=replay, cursor_after=None)

    def _validate_telegram(self, *_a: Any, **_k: Any) -> None:
        """Telegram record/raw delivery validation scripted by the ``telegram_validate`` knob."""
        if self.knobs["telegram_validate"] == "bad":
            raise ValueError("mismatch")

    async def run(self) -> Any:
        """Invoke the function under test with the case keywords."""
        return await ingestion.accept_native_collection(self.session, self.payload, **self.default_kwargs())


async def outcome(monkeypatch: pytest.MonkeyPatch, case: Case) -> tuple[Harness, Any, DomainError | None]:
    """Run a case, returning the harness plus either the receipt or the raised DomainError."""
    harness = Harness(monkeypatch, case)
    try:
        return harness, await harness.run(), None
    except DomainError as exc:
        return harness, None, exc


PROVIDERS = ("telegram", "alpha_vantage", "github", "google_mail", "reddit", "notion")
# Collaborators that precede the state lock for every provider, in the documented order.
HEAD = {
    "telegram": ["lock_source_projection", "collection_fence", "native_credential", "lock_source_credentials",
                 "authorize_collection"],
    "alpha_vantage": ["lock_source_projection", "collection_fence", "world_operation", "lock_source_credentials",
                      "authorize_collection"],
    "github": ["lock_source_projection", "collection_fence", "lock_source_credentials", "authorize_collection",
               "github_binding_fence"],
    "google_mail": ["lock_source_projection", "collection_fence", "lock_source_credentials", "authorize_collection",
                    "scalar:ConnectorOAuthGrant", "scalar:ConnectorProvisioning"],
    "reddit": ["lock_source_projection", "collection_fence", "lock_source_credentials", "authorize_collection",
               "scalar:ConnectorOAuthGrant", "scalar:ConnectorProvisioning"],
    "notion": ["lock_source_projection", "collection_fence", "provider_revision", "lock_source_credentials",
               "authorize_collection"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_empty_new_work_orders_locks_and_never_commits(monkeypatch, provider):
    """New empty page: grant fences, then state lock, replay lookup, inserts, result, settle, publish last."""
    harness, receipt, error = await outcome(monkeypatch, Case(provider))
    assert error is None
    inserts = ["add:IngestionBatch", "flush", "add:IngestionRun", "flush", "add:IngestionStage", "flush"]
    reservation = ["scalar:SourceIngestionState", "scalar:IngestionBatch"]
    # GitHub revalidates the segment after the reservation checks and before any insert.
    middle = {"github": ["github_segment"], "telegram": ["classify_telegram_probe"]}.get(provider, [])
    expected = reservation + middle + inserts + ["record_collection_result", "settle_request", "commit_with_replay"]
    assert harness.calls[len(HEAD[provider]):] == expected
    assert harness.calls[: len(HEAD[provider])] == HEAD[provider]
    assert harness.calls[-1] == "commit_with_replay"
    assert receipt.status == ("succeeded" if provider == "telegram" else "no_changes")
    assert receipt.record_count == 0 and receipt.workspace_id == WORKSPACE and receipt.actor_user_id == 7
    assert receipt.cursor_after == (None if provider == "telegram" else "c1")
    assert harness.state.collection_lease_token is None and harness.state.lease_run_id is None
    assert harness.state.lease_expires_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("field_name,value", [
    ("source_id", uuid4()), ("source_generation", 4), ("lease_token", uuid4()), ("connector_revision", 8),
    ("cursor_before", "zzz"),
])
async def test_lease_mismatch_fails_before_any_lock(monkeypatch, provider, field_name, value):
    """A payload that differs from the owner-issued lease is a 409 before any collaborator runs."""
    harness, receipt, error = await outcome(monkeypatch, Case(provider, payload={field_name: value}))
    assert receipt is None and error.status_code == 409
    assert error.detail == "Native collection payload differs from original lease"
    assert harness.calls == []


@pytest.mark.asyncio
async def test_lease_scope_mismatch_is_404_before_any_lock(monkeypatch):
    """The lease principal/epoch must equal the admitted scope exactly."""
    case = Case("notion", lease={"actor_user_id": 8})
    harness, _, error = await outcome(monkeypatch, case)
    assert error.status_code == 404 and error.detail == "Collection reservation not found"
    assert harness.calls == []


@pytest.mark.asyncio
async def test_missing_source_and_inactive_generation(monkeypatch):
    """404 for an absent projection, 409 when status/local-only/generation moved; nothing else is called."""
    harness, _, error = await outcome(monkeypatch, Case("notion", knobs={"source": "missing"}))
    assert (error.status_code, error.detail) == (404, "Source not found")
    assert harness.calls == ["lock_source_projection"]
    for attr, value in (("status", "paused"), ("local_only", True), ("generation", 9)):
        harness = Harness(monkeypatch, Case("notion"))
        setattr(harness.source, attr, value)
        with pytest.raises(DomainError) as exc:
            await harness.run()
        assert (exc.value.status_code, exc.value.detail) == (409, "Source generation changed during collection")
        assert harness.calls == ["lock_source_projection"]


@pytest.mark.asyncio
async def test_non_native_provider_is_rejected(monkeypatch):
    """A provider outside the native registry fails before the collection fence."""
    harness = Harness(monkeypatch, Case("notion"))
    harness.source.provider = "rss"
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (409, "Native provider is not configured")
    assert harness.calls == ["lock_source_projection"]


CAPTURE_CASES = [
    ("telegram", {"expected_native_operation_id": None}, "Original Telegram credential operation is required"),
    ("telegram", {"expected_world_credential_operation_id": WORLD_OP}, "Original Telegram credential operation is required"),
    ("alpha_vantage", {"expected_world_credential_operation_id": None}, "Original Alpha credential operation is required"),
    ("alpha_vantage", {"expected_native_operation_id": NATIVE_OP}, "Original Alpha credential operation is required"),
    ("github", {"expected_native_operation_id": NATIVE_OP}, "Credential operation capture is not valid for this provider"),
    ("google_mail", {"expected_world_credential_operation_id": WORLD_OP}, "Credential operation capture is not valid for this provider"),
    ("reddit", {"expected_native_operation_id": NATIVE_OP}, "Credential operation capture is not valid for this provider"),
    ("notion", {"expected_world_credential_operation_id": WORLD_OP}, "Credential operation capture is not valid for this provider"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,kwargs,detail", CAPTURE_CASES)
async def test_operation_capture_shape_is_422_before_the_collection_fence(monkeypatch, provider, kwargs, detail):
    """Inapplicable or missing capture shapes fail 422 right after the Source projection, before any fence."""
    harness, _, error = await outcome(monkeypatch, Case(provider, kwargs=kwargs))
    assert (error.status_code, error.detail) == (422, detail)
    assert harness.calls == ["lock_source_projection"]


@pytest.mark.asyncio
async def test_stale_collection_fence_is_409(monkeypatch):
    """The connector collection fence is checked under lock before provider proofs."""
    harness, _, error = await outcome(monkeypatch, Case("notion", knobs={"collection_fence": False}))
    assert (error.status_code, error.detail) == (409, "Connector collection fence is stale")
    assert harness.calls == ["lock_source_projection", "collection_fence"]


REVISION_CASES = [
    ("notion", {"provider_credential_revision": None}, 422, "Provider credential revision proof does not match the source"),
    ("slack", {"provider_credential_revision": None}, 422, "Provider credential revision proof does not match the source"),
    ("reddit", {"provider_credential_revision": 3}, 422, "Provider credential revision proof does not match the source"),
    ("github", {"provider_credential_revision": 3}, 422, "Provider credential revision proof does not match the source"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,kwargs,status,detail", REVISION_CASES)
async def test_rest_provider_revision_proof_shape(monkeypatch, provider, kwargs, status, detail):
    """Revisioned REST providers require the proof; every other native provider must not carry one."""
    harness, _, error = await outcome(monkeypatch, Case(provider, kwargs=kwargs))
    assert (error.status_code, error.detail) == (status, detail)
    assert harness.calls == ["lock_source_projection", "collection_fence"]


@pytest.mark.asyncio
async def test_optional_revision_provider_accepts_both_shapes(monkeypatch):
    """Mastodon may omit or carry the revision proof; carrying it triggers the in-UoW validation."""
    for revision, expected in ((None, False), (5, True)):
        harness = Harness(monkeypatch, Case("notion", kwargs={"provider_credential_revision": revision}))
        harness.source.provider = "mastodon"
        harness.payload = harness.payload.model_copy(update={"provider_credential_revision": revision})
        await harness.run()
        assert ("provider_revision" in harness.calls) is expected


@pytest.mark.asyncio
async def test_revision_proof_changed_is_409(monkeypatch):
    """A rotated provider credential revision fails after the fence and before the credential locks."""
    harness, _, error = await outcome(monkeypatch, Case("notion", knobs={"revision_ok": False}))
    assert (error.status_code, error.detail) == (409, "Provider credential changed during collection")
    assert harness.calls == ["lock_source_projection", "collection_fence", "provider_revision"]


TELEGRAM_ADMISSION = [
    ({"credential": "missing"}, 409, "Native Telegram credential is not ready"),
    ({"credential": "op-changed"}, 409, "Native Telegram credential is not ready"),
    ({"credential": "fence-changed"}, 409, "Native Telegram credential is not ready"),
    ({"credential": "not-ready"}, 409, "Native Telegram credential is not ready"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("knobs,status,detail", TELEGRAM_ADMISSION)
async def test_telegram_credential_recheck(monkeypatch, knobs, status, detail):
    """The native credential snapshot must equal the captured operation and access fence before collector locks."""
    harness, _, error = await outcome(monkeypatch, Case("telegram", knobs=knobs))
    assert (error.status_code, error.detail) == (status, detail)
    assert harness.calls == ["lock_source_projection", "collection_fence", "native_credential"]


@pytest.mark.asyncio
async def test_telegram_update_identity_and_foreign_proofs(monkeypatch):
    """A raw delivery whose update id differs from its body is 422; Telegram proof on other providers is 422."""
    raw = _raw()
    bad = TelegramRawDelivery.model_construct(update_id=12, raw_update_sha256=raw.raw_update_sha256, update=raw.update)
    harness = Harness(monkeypatch, Case("telegram"))
    harness.payload = harness.payload.model_copy(update={"telegram_raw_deliveries": (bad,)})
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (422, "Telegram update identity is invalid")
    for provider in ("notion", "github", "alpha_vantage", "reddit"):
        harness = Harness(monkeypatch, Case(provider))
        harness.payload = harness.payload.model_copy(update={"telegram_raw_deliveries": (raw,)})
        with pytest.raises(DomainError) as exc:
            await harness.run()
        assert (exc.value.status_code, exc.value.detail) == (422, "Telegram proof is not valid for this provider")
        assert "lock_source_credentials" not in harness.calls


@pytest.mark.asyncio
async def test_alpha_original_operation_changed_is_409(monkeypatch):
    """The world credential operation must still match the captured one before collector locks."""
    harness, _, error = await outcome(monkeypatch, Case("alpha_vantage", knobs={"world_ok": False}))
    assert (error.status_code, error.detail) == (409, "Original Alpha credential operation changed")
    assert harness.calls == ["lock_source_projection", "collection_fence", "world_operation"]


@pytest.mark.asyncio
async def test_github_proof_shape_errors(monkeypatch):
    """Missing/stale GitHub proof and GitHub proof on another provider fail before credential locks."""
    harness = Harness(monkeypatch, Case("github"))
    harness.payload = harness.payload.model_copy(update={"github_segment": None})
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (422, "GitHub collection proof is required")
    assert harness.calls == ["lock_source_projection", "collection_fence"]

    harness = Harness(monkeypatch, Case("github"))
    harness.payload = harness.payload.model_copy(update={"connector_revision": 2})
    stale = harness.github_proof.model_copy(update={"fence": harness.github_proof.fence.model_copy(update={"connector_revision": 1})})
    harness.payload = harness.payload.model_copy(update={"github_segment": stale})
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (409, "GitHub collection revision is stale")

    harness = Harness(monkeypatch, Case("notion"))
    harness.payload = harness.payload.model_copy(update={"github_segment": _github_proof()})
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (422, "GitHub proof is not valid for this provider")
    assert "lock_source_credentials" not in harness.calls


VISIBILITY_CASES = [
    # (intent, locator_kind, target_outcome, needs visibility fence)
    ("refresh", "number", "found", False),
    ("refresh", "number", "not_found", True),
    ("refresh", "sha", "forbidden", True),
    ("delete_candidate", "release_id", "partial", True),
    ("reconcile", "repository", "not_found", True),
    ("reconcile", "repository", "found", False),
    ("visibility_check", "installation", "not_found", True),
    ("visibility_check", "ref", "not_found", False),
    ("refresh", "ref", "forbidden", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,locator_kind,target,needs_fence", VISIBILITY_CASES)
async def test_github_visibility_preparation_does_not_reenter_credential_locks(
    monkeypatch, intent, locator_kind, target, needs_fence,
):
    """A possible visibility pause prepares the Source/credential set itself, so ``lock_source_credentials`` is skipped."""
    hint = _hint(intent, locator_kind)
    case = Case("github", knobs={"proof_overrides": {"hint_claim": hint, "target_outcome": target}})
    harness, _, _error = await outcome(monkeypatch, case)
    head = harness.calls[:5]
    if needs_fence:
        assert head == ["lock_source_projection", "collection_fence", "prepare_pause", "authorize_collection",
                        "github_binding_fence"]
        assert "lock_source_credentials" not in harness.calls
    else:
        assert head == ["lock_source_projection", "collection_fence", "lock_source_credentials",
                        "authorize_collection", "github_binding_fence"]
        assert "prepare_pause" not in harness.calls


@pytest.mark.asyncio
async def test_github_pause_path_orders_hint_lock_ack_pause_and_publishes_last(monkeypatch):
    """Visibility pause: hint lock -> ack(in UoW) -> pause Source; no collection result; replay head last."""
    hint = _hint("refresh", "number")
    case = Case("github", payload={"cursor_after": None}, knobs={"proof_overrides": {"hint_claim": hint, "target_outcome": "not_found"}})
    harness, receipt, error = await outcome(monkeypatch, case)
    assert error is None
    tail = harness.calls[harness.calls.index("github_segment"):]
    assert [c for c in tail if not c.startswith(("add:", "flush"))] == [
        "github_segment", "github_hints_lock", "github_ack_in_uow", "pause_source", "settle_request", "commit_with_replay",
    ]
    assert "record_collection_result" not in harness.calls
    assert receipt.status == "no_changes"


@pytest.mark.asyncio
async def test_github_non_pause_ack_uses_plain_acknowledge(monkeypatch):
    """A found target acknowledges through the plain helper and still records the collection result."""
    hint = _hint("refresh", "number")
    case = Case("github", knobs={"proof_overrides": {"hint_claim": hint, "target_outcome": "found"}})
    harness, _, error = await outcome(monkeypatch, case)
    assert error is None
    assert "github_ack" in harness.calls and "github_ack_in_uow" not in harness.calls
    assert harness.calls.index("github_ack") < harness.calls.index("record_collection_result")
    assert harness.calls[-3:] == ["record_collection_result", "settle_request", "commit_with_replay"]


@pytest.mark.asyncio
@pytest.mark.parametrize("knobs,detail", [
    ({"hint_ack": False}, "GitHub hint claim changed during acceptance"),
    ({"paused": "none"}, "GitHub source changed during visibility confirmation"),
])
async def test_github_hint_and_pause_failures_are_409(monkeypatch, knobs, detail):
    """An ack that no longer owns the claim, or a Source that moved before pausing, aborts acceptance."""
    hint = _hint("refresh", "number")
    overrides = {"hint_claim": hint, "target_outcome": "not_found"}
    case = Case("github", payload={"cursor_after": None}, knobs={"proof_overrides": overrides, **knobs})
    harness, _, error = await outcome(monkeypatch, case)
    assert (error.status_code, error.detail) == (409, detail)
    assert "commit_with_replay" not in harness.calls


@pytest.mark.asyncio
async def test_github_grant_fence_unavailable_or_changed(monkeypatch):
    """The GitHub binding fence must exist and equal the proof's fence, after the collector check."""
    for mode in ("missing", "different"):
        harness, _, error = await outcome(monkeypatch, Case("github", knobs={"github_fence": mode}))
        assert (error.status_code, error.detail) == (409, "GitHub grant is unavailable or requires reconnection")
        assert harness.calls == HEAD["github"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,detail", [
    ("cursor", "GitHub collection transition is invalid"),
    ("coverage", "GitHub collection transition is invalid"),
])
async def test_github_segment_transition_must_match_payload(monkeypatch, mode, detail):
    """Supplied cursor/coverage/records must equal the owner-validated segment."""
    harness, _, error = await outcome(monkeypatch, Case("github", knobs={"segment": mode}))
    assert (error.status_code, error.detail) == (409, detail)
    assert harness.calls[-1] == "github_segment"


@pytest.mark.asyncio
async def test_github_visibility_pause_requires_an_empty_unchanged_segment(monkeypatch):
    """A paused receipt cannot move the cursor that this same transaction retires."""
    hint = _hint("refresh", "number")
    overrides = {"hint_claim": hint, "target_outcome": "not_found"}
    # The payload cursor equals the validated segment cursor but differs from the reserved (None) cursor.
    harness, _, error = await outcome(monkeypatch, Case("github", knobs={"proof_overrides": overrides}))
    assert (error.status_code, error.detail) == (409, "GitHub visibility pause requires an empty unchanged segment")
    assert "github_hints_lock" not in harness.calls


GRANT_CASES = [
    ("google_mail", "Google grant changed during collection"),
    ("reddit", "Reddit grant changed during collection"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,detail", GRANT_CASES)
async def test_oauth_grant_recheck_after_collector_before_state(monkeypatch, provider, detail):
    """Grant row + credential revision are read after the collector check and before the state lock."""
    harness, _, error = await outcome(monkeypatch, Case(provider, knobs={"grant": "stale"}))
    assert (error.status_code, error.detail) == (409, detail)
    assert harness.calls == HEAD[provider]


@pytest.mark.asyncio
async def test_google_and_reddit_proof_shape(monkeypatch):
    """Google/Reddit proofs are required for their providers and rejected for every other provider."""
    cases = [
        ("google_mail", {"google_operation_id": None, "google_token_revision": None}, "Google credential proof does not match the provider"),
        ("google_mail", {"google_token_revision": None}, "Google credential proof does not match the provider"),
        ("notion", {"google_operation_id": uuid4(), "google_token_revision": 1}, "Google credential proof does not match the provider"),
        ("reddit", {"reddit_operation_id": None, "reddit_token_revision": None}, "Reddit credential proof does not match the provider"),
        ("notion", {"reddit_operation_id": uuid4(), "reddit_token_revision": 1}, "Reddit credential proof does not match the provider"),
        ("github", {"reddit_operation_id": uuid4()}, "Reddit credential proof does not match the provider"),
    ]
    for provider, update, detail in cases:
        harness = Harness(monkeypatch, Case(provider))
        harness.payload = harness.payload.model_copy(update=update)
        with pytest.raises(DomainError) as exc:
            await harness.run()
        assert (exc.value.status_code, exc.value.detail) == (422, detail), (provider, update)
        # Shape errors are raised after the collector authorization, before the state lock.
        assert "authorize_collection" in harness.calls and "scalar:SourceIngestionState" not in harness.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("replay,holds_token", [("same", True), ("same", False)])
async def test_replay_returns_existing_receipt_without_token_or_cursor_recheck(monkeypatch, provider, replay, holds_token):
    """Replay lookup follows the grant fences and precedes token/cursor recheck; no second batch is inserted."""
    state = {} if holds_token else {"collection_lease_token": None, "lease_expires_at": None, "cursor": "stale-cursor"}
    harness, receipt, error = await outcome(monkeypatch, Case(provider, replay=replay, state=state))
    assert error is None
    assert harness.calls[: len(HEAD[provider])] == HEAD[provider]
    tail = harness.calls[len(HEAD[provider]):]
    assert tail[:3] == ["scalar:SourceIngestionState", "scalar:IngestionBatch", "scalar:IngestionRun"] or (
        provider == "github" and tail[:3] == ["scalar:SourceIngestionState", "scalar:IngestionBatch", "scalar:IngestionRun"]
    )
    assert not any(c.startswith("add:") for c in harness.calls)
    assert "github_segment" not in harness.calls
    if holds_token:
        assert tail[3:] == ["settle_request", "commit_with_replay"]
        assert harness.state.collection_lease_token is None and harness.state.lease_expires_at is None
    else:
        assert tail[3:] == []
        assert "commit_with_replay" not in harness.calls
    assert receipt.batch_id is not None and receipt.run_id is not None
    assert receipt.cursor_after == harness.payload.cursor_after


@pytest.mark.asyncio
async def test_replay_conflict_and_missing_run(monkeypatch):
    """A different payload hash under the same batch key is 409; a receipt without a run is a hard error."""
    harness, _, error = await outcome(monkeypatch, Case("notion", replay="other"))
    assert (error.status_code, error.detail) == (409, "Native receipt conflicts with an existing batch")
    harness = Harness(monkeypatch, Case("notion", replay="same"))
    harness.session.run = None
    with pytest.raises(RuntimeError, match="Native ingestion batch has no run"):
        await harness.run()


RESERVATION_CASES = [
    ({"collection_lease_token": uuid4()}, "Native collection reservation is stale"),
    ({"lease_run_id": uuid4()}, "Native collection reservation is stale"),
    ({"lease_expires_at": NOW - timedelta(seconds=1)}, "Native collection reservation expired"),
    ({"lease_expires_at": None}, "Native collection reservation expired"),
    ({"lease_expires_at": NOW + timedelta(minutes=9)}, "Native collection reservation expired"),
    ({"cursor": "moved-cursor"}, "Native collection cursor is stale"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("notion", "reddit", "alpha_vantage"))
@pytest.mark.parametrize("state,detail", RESERVATION_CASES)
async def test_new_work_rechecks_token_expiry_and_cursor(monkeypatch, provider, state, detail):
    """Only new work requires no run, exact token, unexpired lease equal to the actual lease, and the reserved cursor."""
    harness, _, error = await outcome(monkeypatch, Case(provider, state=state))
    assert (error.status_code, error.detail) == (409, detail)
    assert harness.calls[-2:] == ["scalar:SourceIngestionState", "scalar:IngestionBatch"]
    assert not any(c.startswith("add:") for c in harness.calls)


@pytest.mark.asyncio
async def test_missing_state_row_is_a_stale_reservation(monkeypatch):
    """No state row at all means the reservation does not exist."""
    harness = Harness(monkeypatch, Case("notion"))
    harness.session.state = None
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (409, "Native collection reservation is stale")


@pytest.mark.asyncio
async def test_batch_key_is_bounded(monkeypatch):
    """A provider name that overflows the 255-char batch key is rejected before the state lock."""
    harness = Harness(monkeypatch, Case("notion", kwargs={"provider_credential_revision": None}))
    harness.source.provider = "x" * 300
    monkeypatch.setattr(connectors, "is_native_provider", lambda provider: True)
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (422, "Native batch key exceeds its bound")
    assert "scalar:SourceIngestionState" not in harness.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("notion", "reddit", "google_mail"))
async def test_record_validation_metadata_identity_and_coverage(monkeypatch, provider):
    """Provider metadata must parse and agree with the record identity, version, provider and batch coverage."""
    for flag, detail in (("bad_identity", "Native provider identity is invalid"),
                         ("bad_coverage", "Native provider coverage is inconsistent")):
        case = Case(provider, records="provider", knobs={"record_flags": {flag: True}})
        harness, _, error = await outcome(monkeypatch, case)
        assert (error.status_code, error.detail) == (422, detail)
        assert not any(c.startswith("add:") for c in harness.calls)
    harness = Harness(monkeypatch, Case(provider, records="provider"))
    record = harness.payload.records[0].model_copy(update={"metadata": {"provider_record": {"bad": 1}}})
    harness.payload = harness.payload.model_copy(update={"records": [record]})
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (422, "Native provider metadata is invalid")

    harness = Harness(monkeypatch, Case(provider, records="provider"))
    other = harness.source.provider
    harness.source.provider = "notion" if other != "notion" else "reddit"
    monkeypatch.setattr(connectors, "is_native_provider", lambda p: True)
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert exc.value.status_code in {409, 422}  # proof-shape mismatch raised by the swapped provider


@pytest.mark.asyncio
async def test_new_records_queue_normalization_and_hold_the_run_lease(monkeypatch):
    """With records: stage requested event + normalization scheduled; lease moves to the run; publish last."""
    harness, receipt, error = await outcome(monkeypatch, Case("notion", records="provider"))
    assert error is None
    assert receipt.status == "queued" and receipt.record_count == 1
    assert "add:SourceObservation" in harness.calls
    order = [c for c in harness.calls if c in {"publish_event", "schedule_normalization", "record_collection_result",
                                               "settle_request", "commit_with_replay", "add:SourceObservation"}]
    assert order == ["add:SourceObservation", "publish_event", "schedule_normalization", "record_collection_result",
                     "settle_request", "commit_with_replay"]
    assert harness.state.lease_run_id is not None and harness.state.collection_lease_token is None
    assert harness.state.lease_expires_at is not None and harness.state.cursor == "c1"


@pytest.mark.asyncio
async def test_already_seen_records_take_the_empty_succeeded_path(monkeypatch):
    """When every record key is already stored the batch is accepted as succeeded with no normalization."""
    key = ingestion.native_record_key(_record("notion"))
    harness, receipt, error = await outcome(monkeypatch, Case("notion", records="provider", knobs={"seen": {key}}))
    assert error is None
    assert receipt.status == "no_changes" and receipt.record_count == 0
    assert "publish_event" not in harness.calls and "add:SourceObservation" not in harness.calls
    assert harness.state.lease_run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,detail", [
    ("conflict", "telegram_stream_conflict"),
    ("stale-proof", "Telegram delivery proof is stale or invalid"),
])
async def test_telegram_classification_recheck_after_reservation(monkeypatch, mode, detail):
    """Proofs are recomputed from raw updates only after the live reservation is confirmed."""
    harness, _, error = await outcome(monkeypatch, Case("telegram", knobs={"classification": mode}))
    assert (error.status_code, error.detail) == (409, detail)
    assert harness.calls[-1] == "classify_telegram_probe"
    assert harness.calls[-3:-1] == ["scalar:SourceIngestionState", "scalar:IngestionBatch"]


@pytest.mark.asyncio
async def test_telegram_cursor_transition_must_match(monkeypatch):
    """A cursor that differs from the one recomputed from classification is rejected."""
    _, _, error = await outcome(monkeypatch, Case("telegram", payload={"cursor_after": "other"}))
    assert (error.status_code, error.detail) == (409, "Telegram cursor transition is invalid")


@pytest.mark.asyncio
async def test_telegram_record_proof_validation_and_replay_skip(monkeypatch):
    """Telegram records need a proof+raw+bot+chat match; known replays are skipped; mapper mismatch is 422."""
    harness, receipt, error = await outcome(monkeypatch, Case("telegram", records="provider"))
    assert error is None and receipt.status == "queued" and receipt.received_update_count == 1
    obs = next(o for o in harness.session.added if type(o).__name__ == "SourceObservation")
    assert obs.payload["_native_telegram"]["proof"]["update_id"] == 11
    assert obs.payload["_native_telegram"]["raw_update"]["update_id"] == 11
    assert "validate_telegram_record" in harness.calls

    harness, _, error = await outcome(monkeypatch, Case("telegram", records="provider", knobs={"telegram_validate": "bad"}))
    assert (error.status_code, error.detail) == (422, "Telegram document does not match its raw delivery")

    harness, receipt, error = await outcome(monkeypatch, Case("telegram", records="provider", knobs={"classification": "replay"}))
    assert error is None
    assert receipt.record_count == 0 and receipt.status == "succeeded"  # replayed update acknowledged, no version

    harness = Harness(monkeypatch, Case("telegram", records="provider"))
    harness.source.configuration = {"telegram_chat_ids": ["-999"]}
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (422, "Telegram document proof is invalid")


@pytest.mark.asyncio
async def test_oversized_telegram_observation_is_413(monkeypatch):
    """A normalized row above the 4 MiB single-record budget is refused before any insert."""
    harness = Harness(monkeypatch, Case("telegram", records="provider"))
    big = harness.payload.records[0].model_copy(update={"content": "x" * 1_000_000})
    huge = harness.payload.telegram_raw_deliveries[0].model_copy(update={"update": {"update_id": 11, "blob": "y" * (4 * 1024 * 1024 + 1)}})
    harness.payload = harness.payload.model_copy(update={"records": [big], "telegram_raw_deliveries": (huge,)})
    with pytest.raises(DomainError) as exc:
        await harness.run()
    assert (exc.value.status_code, exc.value.detail) == (413, "Telegram observation exceeds normalization size limit")
    assert not any(c.startswith("add:") for c in harness.calls)


@pytest.mark.asyncio
async def test_request_ref_flows_to_authorization_and_settlement(monkeypatch):
    """A request-bound caller's ref reaches both the collector authorization and the receipt settlement."""
    seen: dict[str, Any] = {}
    harness = Harness(monkeypatch, Case("notion"))
    for module in _owner_modules():
        if hasattr(module, "_authorize_collection"):
            async def authorize(*_a: Any, **kw: Any) -> None:
                harness.calls.append("authorize_collection")
                seen["authorize"] = kw
            monkeypatch.setattr(module, "_authorize_collection", authorize)
        if hasattr(module, "_settle_request"):
            async def settle(*_a: Any, **kw: Any) -> None:
                harness.calls.append("settle_request")
                seen["settle"] = kw
            monkeypatch.setattr(module, "_settle_request", settle)
    ref = SimpleNamespace(request_id=uuid4(), admission_token=uuid4())
    await ingestion.accept_native_collection(harness.session, harness.payload, **{**harness.default_kwargs(), "request_ref": ref})
    assert seen["authorize"]["request_ref"] is ref and seen["authorize"]["collector_token"] == "tok"
    assert seen["settle"]["outcome"] == "no_changes" and seen["settle"]["cursor_after"] == "c1"
    assert isinstance(seen["settle"]["batch_id"], UUID)


@pytest.mark.asyncio
async def test_registry_selects_verifier_by_provider_and_accepts_new_providers(monkeypatch):
    """A verifier registered for a new provider id is used without editing the orchestrator."""
    from modules.ingestion import _native_verifiers as verifiers

    assert isinstance(verifiers.native_verifier_for("telegram"), verifiers.TelegramVerifier)
    assert isinstance(verifiers.native_verifier_for("google_drive"), verifiers.GoogleOAuthVerifier)
    assert verifiers.native_verifier_for("never-heard-of-it").revision_policy == "forbidden"

    seen: list[str] = []

    class AcmeVerifier(verifiers.BaseNativeVerifier):
        """Records that its admission hook ran."""

        providers = ("acme",)

        async def _admit_provider(self, ctx):
            seen.append("admit")

    monkeypatch.setattr(verifiers, "_REGISTRY", dict(verifiers._REGISTRY))
    verifiers.register_native_verifier(AcmeVerifier())
    harness = Harness(monkeypatch, Case("notion", kwargs={"provider_credential_revision": None}))
    harness.source.provider = "acme"
    monkeypatch.setattr(connectors, "is_native_provider", lambda provider: True)
    receipt = await harness.run()
    assert seen == ["admit"] and receipt.status == "no_changes"


@pytest.mark.asyncio
async def test_params_entry_point_is_equivalent_to_the_legacy_keywords(monkeypatch):
    """The typed parameter object and the keyword wrapper produce the same receipt and call order."""
    from modules.ingestion import _native_verifiers as verifiers

    legacy = Harness(monkeypatch, Case("telegram"))
    receipt_a = await legacy.run()
    typed = Harness(monkeypatch, Case("telegram"))
    kwargs = typed.default_kwargs()
    for key in ("expected_native_operation_id", "expected_world_credential_operation_id", "provider_credential_revision"):
        kwargs.pop(key)
    receipt_b = await ingestion.accept_native_collection_with_params(
        typed.session, typed.payload, params=verifiers.NativeProviderParams.telegram(NATIVE_OP), **kwargs,
    )
    assert typed.calls == legacy.calls
    assert receipt_a.model_dump(exclude={"batch_id", "run_id"}) == receipt_b.model_dump(exclude={"batch_id", "run_id"})
