"""Characterization tests for ``seed_demo`` phase ordering, receipts and report arithmetic."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from modules.knowledge.documents import seed
from modules.knowledge.documents.models import Document, DocumentVersion

OWNER = 7
WORKSPACE = uuid4()


class _Harness:
    """Patch every collaborator of ``seed_demo`` and record the order of calls."""

    def __init__(
        self, *, claims: dict[str, bool], source_inserted: bool, existing_docs: int = 0
    ) -> None:
        self.calls: list[str] = []
        self.claims = claims
        self.source_inserted = source_inserted
        self.session = MagicMock()
        self.session.add = MagicMock(
            side_effect=lambda obj: self.calls.append(f"add:{type(obj).__name__}")
        )
        self.session.scalar = AsyncMock(return_value=existing_docs)

        @asynccontextmanager
        async def begin():  # type: ignore[no-untyped-def]
            self.calls.append("begin")
            yield
            self.calls.append("end")

        self.session.begin = begin

    def _rec(self, name: str, result):  # type: ignore[no-untyped-def]
        async def fn(*_a, **_k):  # type: ignore[no-untyped-def]
            self.calls.append(name)
            return result

        return fn

    def patches(self):  # type: ignore[no-untyped-def]
        async def claim(_session, _owner, namespace, *, workspace_id):  # type: ignore[no-untyped-def]
            self.calls.append(f"claim:{namespace}")
            assert workspace_id == WORKSPACE
            return self.claims[namespace]

        async def receipt(_session, _owner, namespace, *, workspace_id):  # type: ignore[no-untyped-def]
            self.calls.append(f"receipt:{namespace}")

        account = SimpleNamespace(default_workspace_id=WORKSPACE)
        owner_ctx = SimpleNamespace(user_id=OWNER, membership_revision=3)
        return [
            patch.object(seed, "Settings", lambda: SimpleNamespace(multi_workspace_enabled=False)),
            patch.object(seed, "get_demo_owner_id", self._rec("owner", OWNER)),
            patch.object(seed, "get_active_account", self._rec("account", account)),
            patch.object(
                seed, "resolve_workspace_owner_context", self._rec("workspace_owner", owner_ctx)
            ),
            patch.object(seed, "claim_demo_seed", claim),
            patch.object(seed, "record_demo_seed_receipt", receipt),
            patch.object(seed, "ensure_demo_goals", self._rec("goals", (1, 10))),
            patch.object(seed, "ensure_demo_tasks", self._rec("tasks", (2, 20))),
            patch.object(seed, "ensure_demo_topics", self._rec("topics", (3, 30))),
            patch.object(seed, "register_modules", list),
            patch.object(seed, "ensure_demo_automations", self._rec("automations", 4)),
            patch.object(seed, "ensure_demo_entities", self._rec("entities", (5, 50))),
            patch.object(seed, "ensure_demo_relationships", self._rec("relationships", (6, 60))),
            patch.object(seed, "ensure_demo_events", self._rec("events", (7, 70))),
            patch.object(seed.documents, "ensure_demo_article", self._rec("article", (8, 80, 800))),
            patch.object(seed, "ensure_demo_conversation", self._rec("conversation", (9, 90, 900))),
            patch.object(
                seed.sources, "ensure_demo_source", self._rec("source", self.source_inserted)
            ),
        ]

    async def run(self):  # type: ignore[no-untyped-def]
        from contextlib import ExitStack

        with ExitStack() as stack:
            for item in self.patches():
                stack.enter_context(item)
            return await seed.seed_demo(self.session)


ALL = {seed.P08_DEMO_NAMESPACE: True, seed.P10_DEMO_NAMESPACE: True, seed.P12_DEMO_NAMESPACE: True}


@pytest.mark.asyncio
async def test_fresh_seed_runs_phases_in_order_and_stages_notes() -> None:
    harness = _Harness(claims=ALL, source_inserted=True)
    report = await harness.run()
    assert harness.calls == [
        "begin",
        "owner",
        "account",
        "workspace_owner",
        f"claim:{seed.P08_DEMO_NAMESPACE}",
        "goals",
        "tasks",
        "topics",
        f"receipt:{seed.P08_DEMO_NAMESPACE}",
        f"claim:{seed.P10_DEMO_NAMESPACE}",
        "automations",
        f"receipt:{seed.P10_DEMO_NAMESPACE}",
        f"claim:{seed.P12_DEMO_NAMESPACE}",
        "entities",
        "relationships",
        "events",
        "article",
        "conversation",
        f"receipt:{seed.P12_DEMO_NAMESPACE}",
        "source",
        "add:Document",
        "add:DocumentVersion",
        "add:Document",
        "add:DocumentVersion",
        "end",
    ]
    assert (report.created, report.existing, report.skipped) == (
        1 + len(seed.NOTES) + (1 + 2 + 3 + 4) + (5 + 6 + 7 + 8 + 9),
        (10 + 20 + 30) + (50 + 60 + 70 + 80 + 90),
        800 + 900,
    )
    assert report.p08_seeded is True and report.p12_seeded is True
    added = [call.args[0] for call in harness.session.add.call_args_list]
    assert isinstance(added[0], Document) and added[0].workspace_id == WORKSPACE
    assert isinstance(added[1], DocumentVersion) and added[1].version_number == 1


@pytest.mark.asyncio
async def test_already_claimed_phases_skip_fixtures_and_existing_source_short_circuits() -> None:
    claims = dict.fromkeys(ALL, False)
    harness = _Harness(claims=claims, source_inserted=False, existing_docs=2)
    report = await harness.run()
    assert not any(
        call.startswith(("goals", "automations", "entities", "receipt", "add:"))
        for call in harness.calls
    )
    assert report.created == 0 and report.existing == 1 + 2
    assert (report.p08_seeded, report.p12_seeded, report.skipped) == (False, False, 0)
    harness.session.add.assert_not_called()


@pytest.mark.asyncio
async def test_missing_account_and_foreign_workspace_owner_are_rejected() -> None:
    harness = _Harness(claims=ALL, source_inserted=True)
    patches = harness.patches()
    patches[2] = patch.object(seed, "get_active_account", AsyncMock(return_value=None))
    from contextlib import ExitStack

    with ExitStack() as stack, pytest.raises(RuntimeError, match="active owner account"):
        for item in patches:
            stack.enter_context(item)
        await seed.seed_demo(harness.session)
    harness = _Harness(claims=ALL, source_inserted=True)
    patches = harness.patches()
    patches[3] = patch.object(
        seed,
        "resolve_workspace_owner_context",
        AsyncMock(return_value=SimpleNamespace(user_id=99, membership_revision=1)),
    )
    with ExitStack() as stack, pytest.raises(RuntimeError, match="default workspace"):
        for item in patches:
            stack.enter_context(item)
        await seed.seed_demo(harness.session)
