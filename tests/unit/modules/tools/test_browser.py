"""Unit tests for browser tool contracts and navigation guards.

Tests browser schemas, URL bounds, navigation safety guards, HMAC job token derivation,
and session tracking in modules.tools.browser_public and modules.tools.browser_control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from modules.agents.public import BrowserRunAuthorization
from modules.connectors.public import AgentBrowserScope
from modules.tools.browser_control import BrowserControlEvent, _service_authorized, _target_in_scope
from modules.tools.browser_public import (
    BrowserPageRead,
    BrowserReadArgs,
    BrowserReadBudget,
    BrowserReadJobRead,
    BrowserReadResult,
    browser_capability_verified,
    cancel_browser_job_in_uow,
    derive_browser_job_token,
    read_browser_result,
    submit_browser_read_in_uow,
)
from modules.tools.models import BrowserPageEvidence, BrowserReadJob


class TestBrowserSchemas:
    """Unit tests for BrowserReadArgs, BrowserPageRead, and BrowserReadResult schemas."""

    def test_browser_capability_verified_defaults_false(self) -> None:
        """Verify browser capability gate defaults to False for fail-closed isolation."""
        assert browser_capability_verified() is False

    def test_browser_read_args_valid(self) -> None:
        """Verify BrowserReadArgs accepts valid source UUID and pages between 1 and 3."""
        src_id = uuid4()
        args = BrowserReadArgs(source_id=src_id, max_pages=2)
        assert args.source_id == src_id
        assert args.max_pages == 2

    def test_browser_read_args_rejects_bounds(self) -> None:
        """Verify BrowserReadArgs rejects max_pages < 1 or > 3."""
        src_id = uuid4()
        with pytest.raises(ValidationError):
            BrowserReadArgs(source_id=src_id, max_pages=0)
        with pytest.raises(ValidationError):
            BrowserReadArgs(source_id=src_id, max_pages=4)

    def test_browser_read_args_forbids_extra(self) -> None:
        """Verify BrowserReadArgs forbids extra fields."""
        with pytest.raises(ValidationError):
            BrowserReadArgs(source_id=uuid4(), max_pages=1, extra_field="forbidden")  # type: ignore

    def test_browser_page_read_extracted_text_limit(self) -> None:
        """Verify BrowserPageRead enforces 20,000 char limit on extracted text."""
        with pytest.raises(ValidationError):
            BrowserPageRead(
                id=uuid4(),
                requested_url="https://example.com",
                final_url="https://example.com",
                observed_at=datetime.now(UTC),
                content_digest="a" * 64,
                extracted_text="x" * 20_001,
            )

    def test_browser_read_result_page_count_limit(self) -> None:
        """Verify BrowserReadResult caps maximum pages at 3."""
        job_read = BrowserReadJobRead(
            id=uuid4(),
            operation_id=uuid4(),
            run_id=uuid4(),
            tool_slot=1,
            source_id=uuid4(),
            status="succeeded",
            actual_pages=4,
            actual_bytes=1000,
            result_hash="b" * 64,
            error_code=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        pages = tuple(
            BrowserPageRead(
                id=uuid4(),
                requested_url=f"https://example.com/{i}",
                final_url=f"https://example.com/{i}",
                observed_at=datetime.now(UTC),
                content_digest="c" * 64,
                extracted_text=f"page {i}",
            )
            for i in range(4)
        )
        with pytest.raises(ValidationError):
            BrowserReadResult(job=job_read, pages=pages)


class TestBrowserJobTokenDerivation:
    """Unit tests for derive_browser_job_token HMAC SHA-256 helper."""

    def test_derive_token_success(self) -> None:
        """Verify derive_browser_job_token creates deterministic hex HMAC."""
        secret = "super-secret-token"
        op_id = uuid4()
        token1 = derive_browser_job_token(secret, op_id, 1)
        token2 = derive_browser_job_token(secret, op_id, 1)
        assert len(token1) == 64
        assert token1 == token2

    def test_derive_token_different_generation(self) -> None:
        """Verify token changes with different claim generation."""
        secret = "super-secret-token"
        op_id = uuid4()
        t1 = derive_browser_job_token(secret, op_id, 1)
        t2 = derive_browser_job_token(secret, op_id, 2)
        assert t1 != t2

    def test_derive_token_invalid_inputs(self) -> None:
        """Verify derive_browser_job_token raises ValueError on empty secret or claim < 1."""
        op_id = uuid4()
        with pytest.raises(ValueError, match="invalid"):
            derive_browser_job_token("", op_id, 1)
        with pytest.raises(ValueError, match="invalid"):
            derive_browser_job_token("secret", op_id, 0)


class TestBrowserNavigationGuards:
    """Unit tests for _target_in_scope URL bounds and navigation safety."""

    def test_target_in_scope_valid_https(self) -> None:
        """Verify valid https target within origin and prefix is accepted."""
        origin = "https://docs.example.com"
        prefix = "/reference"
        target = "https://docs.example.com/reference/api"
        assert _target_in_scope(target, origin, prefix) is True

    def test_target_in_scope_rejects_http(self) -> None:
        """Verify insecure HTTP target is rejected."""
        assert _target_in_scope("http://docs.example.com/api", "https://docs.example.com", "/") is False

    def test_target_in_scope_rejects_non_standard_port(self) -> None:
        """Verify targets on ports other than 443 are rejected."""
        assert _target_in_scope("https://docs.example.com:8443/api", "https://docs.example.com", "/") is False

    def test_target_in_scope_rejects_credentials(self) -> None:
        """Verify target URLs with user or password are rejected."""
        assert _target_in_scope("https://user:pass@docs.example.com/api", "https://docs.example.com", "/") is False

    def test_target_in_scope_rejects_traversal(self) -> None:
        """Verify target URLs with path traversal segments are rejected."""
        assert _target_in_scope("https://docs.example.com/api/../secret", "https://docs.example.com", "/") is False
        assert _target_in_scope("https://docs.example.com/api/./here", "https://docs.example.com", "/") is False

    def test_target_in_scope_rejects_encoded_slashes(self) -> None:
        """Verify target URLs with %2f or %5c encoded characters are rejected."""
        assert _target_in_scope("https://docs.example.com/api%2fsecret", "https://docs.example.com", "/") is False
        assert _target_in_scope("https://docs.example.com/api%5csecret", "https://docs.example.com", "/") is False

    def test_target_in_scope_rejects_outside_prefix(self) -> None:
        """Verify target URL outside path prefix is rejected."""
        origin = "https://example.com"
        prefix = "/allowed/path"
        target = "https://example.com/other/path"
        assert _target_in_scope(target, origin, prefix) is False


class TestBrowserControlEvents:
    """Unit tests for BrowserControlEvent schema validation and service authorization."""

    def test_browser_control_event_valid(self) -> None:
        """Verify BrowserControlEvent parses valid register event."""
        event = BrowserControlEvent(
            event="register",
            operation_id=uuid4(),
            claim_generation=1,
            service_instance_id="srv-instance-123",
            request_ordinal=0,
        )
        assert event.event == "register"
        assert event.claim_generation == 1

    def test_service_authorized_bearer(self) -> None:
        """Verify _service_authorized constant-time compare against shared secret."""
        expected = "shared-secret-123"
        assert _service_authorized("Bearer shared-secret-123", expected) is True
        assert _service_authorized("Bearer wrong-secret", expected) is False
        assert _service_authorized("Basic shared-secret-123", expected) is False
        assert _service_authorized(None, expected) is False


class TestBrowserJobSubmissionAndCancellation:
    """Unit tests for submit_browser_read_in_uow and cancel_browser_job_in_uow."""

    @pytest.fixture
    def mock_agent_auth(self) -> BrowserRunAuthorization:
        """Return a valid BrowserRunAuthorization."""
        source_id = uuid4()
        return BrowserRunAuthorization(
            owner_id=1,
            run_id=uuid4(),
            tool_slot=1,
            arguments_hash="arg-hash",
            auth_session_hash="session-hash",
            conversation_id=uuid4(),
            profile_id="specialist-1",
            profile_revision_hash="rev-hash",
            source_ids=frozenset({source_id}),
            claim_generation=1,
            remaining_jobs=1,
            remaining_pages=3,
            remaining_bytes=1_000_000,
            remaining_active_seconds=30,
        )

    @pytest.fixture
    def mock_browser_scope(self, mock_agent_auth) -> AgentBrowserScope:
        """Return a valid AgentBrowserScope matching the source."""
        source_id = next(iter(mock_agent_auth.source_ids))
        return AgentBrowserScope(
            source_id=source_id,
            source_generation=1,
            connector_revision=1,
            grant_revision=1,
            scope_hash="scope-hash",
            origin="https://example.com",
            path_prefix="/",
            local_only=False,
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_submit_browser_read_rejects_non_owner(
        self, mock_agent_auth, mock_browser_scope
    ) -> None:
        """Verify submit_browser_read_in_uow raises PermissionError when owner_id != 1."""
        session = AsyncMock()
        budget = BrowserReadBudget(
            authorization=mock_agent_auth,
            operation_id=uuid4(),
            max_bytes=100_000,
            max_active_seconds=30,
            service_token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        args = BrowserReadArgs(source_id=mock_browser_scope.source_id, max_pages=1)

        with pytest.raises(PermissionError, match="authorization is invalid"):
            await submit_browser_read_in_uow(
                session=session,
                owner_id=2,  # Not owner 1
                run_id=mock_agent_auth.run_id,
                tool_slot=1,
                auth_session_hash="session-hash",
                scope=mock_browser_scope,
                args=args,
                budget=budget,
            )

    @pytest.mark.asyncio
    async def test_submit_browser_read_rejects_naive_expires_at(
        self, mock_agent_auth, mock_browser_scope
    ) -> None:
        """Verify submit_browser_read_in_uow rejects naive datetime for expires_at."""
        session = AsyncMock()
        budget = BrowserReadBudget(
            authorization=mock_agent_auth,
            operation_id=uuid4(),
            max_bytes=100_000,
            max_active_seconds=30,
            service_token_hash="a" * 64,
            expires_at=datetime.now(),  # naive!
        )
        args = BrowserReadArgs(source_id=mock_browser_scope.source_id, max_pages=1)

        with pytest.raises(PermissionError, match="authorization is invalid"):
            await submit_browser_read_in_uow(
                session=session,
                owner_id=1,
                run_id=mock_agent_auth.run_id,
                tool_slot=1,
                auth_session_hash="session-hash",
                scope=mock_browser_scope,
                args=args,
                budget=budget,
            )

    @pytest.mark.asyncio
    async def test_submit_browser_read_success(
        self, mock_agent_auth, mock_browser_scope
    ) -> None:
        """Verify submit_browser_read_in_uow successfully adds BrowserReadJob row."""
        session = AsyncMock()
        session.scalar.return_value = None  # No existing job
        session.add = MagicMock()

        def simulate_add(obj):
            obj.actual_pages = 0
            obj.actual_bytes = 0

        session.add.side_effect = simulate_add
        budget = BrowserReadBudget(
            authorization=mock_agent_auth,
            operation_id=uuid4(),
            max_bytes=100_000,
            max_active_seconds=30,
            service_token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        args = BrowserReadArgs(source_id=mock_browser_scope.source_id, max_pages=1)

        job_read = await submit_browser_read_in_uow(
            session=session,
            owner_id=1,
            run_id=mock_agent_auth.run_id,
            tool_slot=1,
            auth_session_hash="session-hash",
            scope=mock_browser_scope,
            args=args,
            budget=budget,
        )

        assert job_read.status == "queued"
        assert job_read.run_id == mock_agent_auth.run_id
        session.add.assert_called_once()
        session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_browser_job_success(self) -> None:
        """Verify cancel_browser_job_in_uow sets cancel_requested and status to cancel_requested."""
        session = AsyncMock()
        mock_job = BrowserReadJob(
            id=uuid4(),
            operation_id=uuid4(),
            owner_id=1,
            run_id=uuid4(),
            tool_slot=1,
            auth_session_hash="hash",
            conversation_id=uuid4(),
            profile_id="specialist-1",
            authorized_source_ids=[],
            profile_revision_hash="rev",
            claim_generation=1,
            source_id=uuid4(),
            source_generation=1,
            connector_revision=1,
            grant_revision=1,
            scope_hash="scope",
            arguments_hash="arg",
            max_pages=2,
            max_bytes=1000,
            actual_pages=0,
            actual_bytes=0,
            result_hash=None,
            error_code=None,
            max_active_seconds=30,
            service_token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            status="running",
        )
        session.scalar.return_value = mock_job
        session.execute.return_value = MagicMock(rowcount=0)

        result = await cancel_browser_job_in_uow(session, owner_id=1, job_id=mock_job.id)

        assert result.status == "cancel_requested"
        assert mock_job.cancel_requested is True
        session.flush.assert_awaited_once()
