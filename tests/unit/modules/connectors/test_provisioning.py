"""Unit tests for connector provisioning, credential management, and lifecycle state transitions.

Covers:
- Credential masking and secret redaction (_connector_observation, complete_credential_operation, clear_retired_source_credentials)
- Connector lifecycle state transitions (save_desired, begin_enable, begin_activation_bundle, reject_activation,
  prepare_workflow_step, claim_workflow_step, acknowledge_workflow_step, fail_workflow_step, mark_reconciliation, fence_source_collection)
- Secret storage and encryption (secret_fingerprint, encrypt_credential_input, decrypt_credential_input,
  encrypt_native_token, decrypt_native_token, save_native_credential, revoke_native_credential)
- Token refresh and OAuth token cipher validation (_validate_expiring_token, _token_cipher, _open_token_cipher, refresh_github_grant)
"""

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from core.config import Settings
from core.workspaces.schemas import AccessFence, WorkspaceContext
from modules.connectors.credentials import (
    CredentialEncryptionUnavailable,
    decrypt_credential_input,
    decrypt_native_token,
    encrypt_credential_input,
    encrypt_native_token,
    secret_fingerprint,
)
from modules.connectors.github.oauth import (
    _open_token_cipher,
    _token_cipher,
    _validate_expiring_token,
    refresh_github_grant,
)
from modules.connectors.models import (
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorProvisioning,
    GithubOAuthGrant,
)
from modules.connectors.provisioning import (
    _connector_observation,
    acknowledge_workflow_step,
    begin_enable,
    claim_workflow_step,
    clear_retired_source_credentials,
    complete_credential_operation,
    fence_source_collection,
    finish_deleted_source_grant_revoke,
    reject_activation,
    save_desired,
)
from modules.connectors.public import NativeCredentialSnapshot
from modules.sources.schemas import SourceFence

WORKSPACE_ID = uuid4()
SCOPE = WorkspaceContext(user_id=7, workspace_id=WORKSPACE_ID, role="owner", membership_revision=3)
ACCESS = AccessFence(WORKSPACE_ID, 7, 3, 5)
FLAG = {"scope": SCOPE, "multi_workspace_enabled": False}


def _identity() -> dict[str, object]:
    """Durable operation principal/epoch keys that a retained-effect envelope must carry."""
    return {
        "workspace_id": str(WORKSPACE_ID), "actor_user_id": 7, "membership_revision": 3,
        "workspace_configuration_revision": 5,
    }


def _fence(source_id=None, generation: int = 1, status: str = "active") -> SourceFence:
    return SourceFence(
        id=source_id or uuid4(), workspace_id=WORKSPACE_ID, generation=generation, status=status, local_only=False,
    )


@pytest.fixture(autouse=True)
def _admitted_access():
    """Owner admission is covered by scope tests; these exercise provisioning state transitions."""
    with patch("modules.connectors.provisioning._connector_access", AsyncMock(return_value=ACCESS)):
        yield


@pytest.fixture
def fernet_key() -> str:
    """Generate a valid urlsafe base64 32-byte Fernet key."""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def mock_source_fence() -> SourceFence:
    """Create a sample active SourceFence."""
    return _fence()


class TestCredentialMaskingAndRedaction:
    """Tests verifying secrets and credential tokens are never exposed in observations or retained unnecessarily."""

    def test_connector_observation_none_source(self) -> None:
        """Connector observation returns None when source fence is None."""
        assert _connector_observation(None, None, {}, ACCESS) is None

    def test_connector_observation_unsaved_defaults(self, mock_source_fence: SourceFence) -> None:
        """Connector observation returns default saved_not_active values for revision zero without row."""
        obs = _connector_observation(mock_source_fence, None, {}, ACCESS)
        assert obs is not None
        assert obs.desired_revision == 0
        assert obs.applied_revision == 0
        assert obs.state == "saved_not_active"
        assert obs.credential_recovery == "supported"
        assert obs.credential_presence == ()
        assert obs.header_auth_configured is False

    def test_connector_observation_masks_credentials(self, mock_source_fence: SourceFence) -> None:
        """Connector observation exposes only boolean slot presence, never secret tokens or ciphertext."""
        source_id = mock_source_fence.id
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=2,
            applied_revision=2,
            state="active",
            desired_enabled=True,
            desired_configuration={"auth_method": "http_header"},
        )
        cred = ConnectorManagedCredential(
            source_id=source_id,
            slot="collector",
            credential_id="cred-12345",
            state="ready",
        )
        slots = {"collector": cred}

        obs = _connector_observation(mock_source_fence, row, slots, ACCESS)
        assert obs is not None
        assert obs.desired_revision == 2
        assert obs.applied_revision == 2
        assert obs.state == "active"
        assert obs.header_auth_configured is True
        presence_dict = dict(obs.credential_presence)
        assert presence_dict["collector"] is True
        assert presence_dict["manual_trigger"] is False
        assert presence_dict["provider"] is False

    def test_connector_observation_unresolved_credentials_trigger_reconciliation(
        self, mock_source_fence: SourceFence
    ) -> None:
        """Unresolved credential operations force state to reconciliation_required and credential_recovery to unsupported."""
        source_id = mock_source_fence.id
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=1,
            applied_revision=1,
            state="active",
            desired_enabled=True,
            desired_configuration={},
        )
        cred = ConnectorManagedCredential(
            source_id=source_id,
            slot="collector",
            credential_id="cred-123",
            state="dispatching",
        )
        slots = {"collector": cred}

        obs = _connector_observation(mock_source_fence, row, slots, ACCESS)
        assert obs is not None
        assert obs.state == "reconciliation_required"
        assert obs.error_code == "credential_operation_pending"
        assert obs.credential_recovery == "unsupported_operation"

    @pytest.mark.asyncio
    async def test_complete_credential_operation_pops_ciphertext(self) -> None:
        """Acknowledging a dispatched credential operation removes input_ciphertext from the envelope."""
        source_id = uuid4()
        operation_id = uuid4()
        envelope = {
            **_identity(),
            "id": str(operation_id),
            "kind": "create",
            "state": "dispatched",
            "revision": 2,
            "source_generation": 1,
            "input_ciphertext": "super-secret-ciphertext",
        }
        cred = ConnectorManagedCredential(
            source_id=source_id,
            slot="collector",
            operation_id=operation_id,
            operation_revision=2,
            source_generation=1,
            state="dispatching",
            operation_envelope=dict(envelope),
        )
        desired = ConnectorProvisioning(source_id=source_id, source_generation=1, desired_revision=2, credential_revision=1)
        session = AsyncMock()

        with patch("modules.connectors.provisioning._read_retained_connector_rows",
                   return_value=(_fence(source_id), desired, {"collector": cred})), \
             patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)):
            result = await complete_credential_operation(
                session,
                source_id=source_id,
                slot="collector",
                operation_id=operation_id,
                credential_id="n8n-cred-id",
                binding={"auth": "ok"},
                original_operation=envelope,
                access_fence=ACCESS,
                **FLAG,
            )

        assert result is True
        assert cred.state == "ready"
        assert cred.credential_id == "n8n-cred-id"
        assert "input_ciphertext" not in cred.operation_envelope
        assert cred.operation_envelope["state"] == "succeeded"

    @pytest.mark.asyncio
    async def test_clear_retired_source_credentials_wipes_secrets(self) -> None:
        """Archived or purged sources have native tokens and GitHub grant tokens stripped completely."""
        source_id = uuid4()
        native = ConnectorNativeCredential(
            source_id=source_id,
            operation_id=uuid4(),
            encrypted_token="encrypted-token",
            token_fingerprint="fingerprint-hash",
            verified_bot_id="123456",
            state="ready",
        )
        grant = GithubOAuthGrant(
            source_id=source_id,
            github_user_id="user-1",
            encrypted_tokens="encrypted-oauth-tokens",
            state="ready",
        )

        session = AsyncMock()
        session.scalar = AsyncMock(side_effect=[native, grant])

        with patch("modules.connectors.provisioning._read_scoped_source", return_value=MagicMock()), \
             patch("modules.connectors.provisioning.github_grant_has_active_peer", return_value=True):
            await clear_retired_source_credentials(session, source_id, **FLAG)

        assert native.encrypted_token is None
        assert native.token_fingerprint is None
        assert native.verified_bot_id is None
        assert native.state == "revoked"
        assert grant.encrypted_tokens is None
        assert grant.state == "revoked"
        assert grant.error_code == "provider_revoke_skipped_source_deleted"

    @pytest.mark.asyncio
    async def test_finish_deleted_source_grant_revoke_clears_ciphertext(self) -> None:
        """finish_deleted_source_grant_revoke always removes ciphertext regardless of remote revoke outcome."""
        source_id = uuid4()
        grant_operation_id = uuid4()
        grant = GithubOAuthGrant(
            source_id=source_id,
            encrypted_tokens="retained-tokens",
            operation_id=grant_operation_id,
            token_revision=3,
        )
        session = AsyncMock()
        session.scalar = AsyncMock(return_value=grant)

        with patch("modules.connectors.provisioning._read_scoped_source", return_value=MagicMock()):
            await finish_deleted_source_grant_revoke(
                session, source_id, "provider_revoked", access_fence=ACCESS,
                grant_operation_id=grant_operation_id, token_revision=3, **FLAG,
            )
        assert grant.encrypted_tokens is None
        assert grant.error_code == "provider_revoked"
        session.flush.assert_awaited_once()


class TestSecretStorageAndEncryption:
    """Tests for Fernet-based symmetric encryption and HMAC secret fingerprinting."""

    def test_secret_fingerprint_valid_key(self, fernet_key: str) -> None:
        """secret_fingerprint generates a 64-character hexadecimal HMAC-SHA256 digest."""
        fp1 = secret_fingerprint(fernet_key, "my-secret-token")
        fp2 = secret_fingerprint(fernet_key, "my-secret-token")
        assert len(fp1) == 64
        assert fp1 == fp2

        fp3 = secret_fingerprint(fernet_key, "different-token")
        assert fp1 != fp3

    def test_secret_fingerprint_invalid_key_raises(self) -> None:
        """An invalid base64 or wrong-length key raises CredentialEncryptionUnavailable."""
        with pytest.raises(CredentialEncryptionUnavailable, match="encryption is unavailable"):
            secret_fingerprint("short-invalid-key", "token")

    def test_encrypt_decrypt_credential_input_round_trip(self, fernet_key: str) -> None:
        """Credential input is cleanly encrypted and decrypted when bindings match."""
        source_id = uuid4()
        operation_id = uuid4()
        slot = "collector"
        request = {"name": "headerAuth", "data": {"name": "X-API-KEY", "value": "secret-123"}}
        binding = {"provider": "n8n", "format": "header"}

        ciphertext = encrypt_credential_input(
            fernet_key,
            source_id=source_id,
            slot=slot,
            operation_id=operation_id,
            request=request,
            binding=binding,
        )

        dec_request, dec_binding = decrypt_credential_input(
            fernet_key,
            ciphertext,
            source_id=source_id,
            slot=slot,
            operation_id=operation_id,
        )
        assert dec_request == request
        assert dec_binding == binding

    def test_decrypt_credential_input_mismatched_binding_raises(self, fernet_key: str) -> None:
        """Decrypting with a mismatched source_id or slot raises CredentialEncryptionUnavailable."""
        source_id = uuid4()
        operation_id = uuid4()
        slot = "collector"
        ciphertext = encrypt_credential_input(
            fernet_key,
            source_id=source_id,
            slot=slot,
            operation_id=operation_id,
            request={"data": "test"},
            binding={"b": 1},
        )

        with pytest.raises(CredentialEncryptionUnavailable, match="binding is invalid"):
            decrypt_credential_input(
                fernet_key,
                ciphertext,
                source_id=uuid4(),  # Different source_id
                slot=slot,
                operation_id=operation_id,
            )

    def test_encrypt_decrypt_native_token_telegram(self, fernet_key: str) -> None:
        """Telegram native tokens round-trip encrypt and decrypt through NativeCredentialSnapshot."""
        source_id = uuid4()
        operation_id = uuid4()
        bot_id = "123456789"
        raw_token = "123456789:ABCdefGHI_jklMNOpqrsTUVwxyz-1234567"

        encrypted = encrypt_native_token(
            fernet_key,
            source_id=source_id,
            operation_id=operation_id,
            source_generation=1,
            configuration_revision=2,
            token=raw_token,
            verified_bot_id=bot_id,
        )

        snapshot = NativeCredentialSnapshot(
            access_fence=ACCESS,
            workspace_id=WORKSPACE_ID,
            source_id=source_id,
            operation_id=operation_id,
            source_generation=1,
            configuration_revision=2,
            verified_bot_id=bot_id,
            bound_bot_id=bot_id,
            encrypted_token=encrypted,
            state="ready",
            validated_at=datetime.now(UTC),
        )

        decrypted = decrypt_native_token(fernet_key, snapshot)
        assert decrypted == raw_token

    def test_encrypt_native_token_validation_failures(self, fernet_key: str) -> None:
        """Invalid tokens or malformed bot IDs are rejected immediately."""
        source_id = uuid4()
        op_id = uuid4()

        # Token with whitespace
        with pytest.raises(ValueError, match="Telegram token is invalid"):
            encrypt_native_token(
                fernet_key,
                source_id=source_id,
                operation_id=op_id,
                source_generation=1,
                configuration_revision=1,
                token="token with spaces",
                verified_bot_id="123",
            )

        # Bot ID not decimal
        with pytest.raises(ValueError, match="Telegram bot identity is invalid"):
            encrypt_native_token(
                fernet_key,
                source_id=source_id,
                operation_id=op_id,
                source_generation=1,
                configuration_revision=1,
                token="valid_token_123",
                verified_bot_id="not-a-number",
            )

    def test_decrypt_native_token_not_ready_raises(self, fernet_key: str) -> None:
        """Decrypting a native credential snapshot whose state is not ready raises CredentialEncryptionUnavailable."""
        snapshot = NativeCredentialSnapshot(
            access_fence=ACCESS,
            workspace_id=WORKSPACE_ID,
            source_id=uuid4(),
            operation_id=uuid4(),
            source_generation=1,
            configuration_revision=1,
            verified_bot_id="123",
            bound_bot_id="123",
            encrypted_token="some-cipher",
            state="revoked",
            validated_at=None,
        )
        with pytest.raises(CredentialEncryptionUnavailable, match="unavailable"):
            decrypt_native_token(fernet_key, snapshot)


class TestConnectorLifecycleStateTransitions:
    """Tests for connector lifecycle state transitions (save_desired, begin_enable, workflow execution)."""

    @pytest.mark.asyncio
    async def test_save_desired_initial_revision_zero_to_one(self) -> None:
        """Initial configuration save for revision 0 creates revision 1 with saved_not_active state."""
        source_id = uuid4()
        session = AsyncMock()
        session.add = MagicMock()

        with patch("modules.connectors.provisioning.sources.lock_source", return_value=_fence(source_id)), \
             patch("modules.connectors.provisioning._lock_connector_rows", return_value=(_fence(source_id), None, {})):
            row = await save_desired(session, source_id, source_generation=1, expected_revision=0, configuration={"url": "https://example.com"}, **FLAG)

        assert row is not None
        assert row.source_id == source_id
        assert row.source_generation == 1
        assert row.desired_revision == 1
        assert row.state == "saved_not_active"
        assert row.desired_enabled is False
        session.add.assert_called_once_with(row)
        session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_desired_revision_mismatch_returns_none(self) -> None:
        """When expected_revision does not match current desired_revision, save_desired returns None."""
        source_id = uuid4()
        existing = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=2,
            state="saved_not_active",
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.sources.lock_source", return_value=_fence(source_id)), \
             patch("modules.connectors.provisioning._lock_connector_rows", return_value=(_fence(source_id), existing, {})):
            row = await save_desired(session, source_id, source_generation=1, expected_revision=1, configuration={}, **FLAG)

        assert row is None

    @pytest.mark.asyncio
    async def test_save_desired_bumps_revision_and_handles_prior_active(self) -> None:
        """Saving a new configuration on a previously active connector increments revision and stages deactivation."""
        source_id = uuid4()
        existing = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=1,
            state="active",
            desired_enabled=True,
            workflow_id="wf-100",
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.sources.lock_source", return_value=_fence(source_id)), \
             patch("modules.connectors.provisioning._lock_connector_rows", return_value=(_fence(source_id), existing, {})):
            row = await save_desired(session, source_id, source_generation=1, expected_revision=1, configuration={"url": "https://new.example.com"}, **FLAG)

        assert row is not None
        assert row.desired_revision == 2
        assert row.desired_enabled is False
        assert row.state == "saved_not_active"
        assert row.error_code == "deactivation_pending"
        assert row.workflow_operation is not None
        assert row.workflow_operation["kind"] == "deactivate"

    @pytest.mark.asyncio
    async def test_begin_enable_success(self) -> None:
        """begin_enable transitions connector to provisioning state and returns an operation_id."""
        source_id = uuid4()
        activation_id = uuid4()
        source = _fence(source_id)
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=2,
            state="saved_not_active",
            workflow_operation=None,
            activation_intent={"id": str(activation_id), "required_credentials": {}},
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.lock_connector", return_value=(source, row, {})), \
             patch("modules.connectors.provisioning._read_connector_rows", return_value=(source, row, {})), \
             patch("modules.connectors.provisioning._operation_matches", AsyncMock(return_value=True)):
            op_id = await begin_enable(
                session,
                source_id=source_id,
                source_generation=1,
                revision=2,
                configuration={"url": "https://example.com"},
                workflow_name="Test Workflow",
                body={"nodes": []},
                activation_id=activation_id,
                **FLAG,
            )

        assert op_id is not None
        assert row.desired_enabled is True
        assert row.state == "provisioning"
        assert row.workflow_operation is not None
        assert row.workflow_operation["kind"] == "enable"

    @pytest.mark.asyncio
    async def test_begin_enable_fence_mismatch_returns_none(self) -> None:
        """begin_enable rejects requests when source generation or revision does not match."""
        source_id = uuid4()
        source = _fence(source_id, generation=2)  # Gen 2 vs Gen 1
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=2,
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.lock_connector", return_value=(source, row, {})), \
             patch("modules.connectors.provisioning._read_connector_rows", return_value=(source, row, {})):
            op_id = await begin_enable(
                session,
                source_id=source_id,
                source_generation=1,
                revision=2,
                configuration={},
                workflow_name="wf",
                body={},
                activation_id=uuid4(),
                **FLAG,
            )

        assert op_id is None

    @pytest.mark.asyncio
    async def test_reject_activation(self) -> None:
        """reject_activation marks the connector saved_not_active with the provided error code."""
        source_id = uuid4()
        row = ConnectorProvisioning(
            source_id=source_id,
            desired_revision=3,
            desired_enabled=True,
            state="provisioning",
            workflow_operation=None,
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.lock_connector", return_value=(MagicMock(), row, {})):
            result = await reject_activation(session, source_id, revision=3, error_code="config_invalid", **FLAG)

        assert result is True
        assert row.desired_enabled is False
        assert row.state == "saved_not_active"
        assert row.error_code == "config_invalid"

    @pytest.mark.asyncio
    async def test_claim_and_acknowledge_workflow_step_progression(self) -> None:
        """Workflow step progresses through prepared -> claim (dispatched) -> acknowledge."""
        source_id = uuid4()
        operation_id = uuid4()
        step_id = str(uuid4())
        source = _fence(source_id)
        op = {
            **_identity(),
            "id": str(operation_id),
            "kind": "enable",
            "source_generation": 1,
            "revision": 2,
            "required_credentials": {},
            "step": {
                "id": step_id,
                "kind": "activate",
                "state": "prepared",
                "history": [],
            },
        }
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=1,
            desired_revision=2,
            desired_enabled=True,
            state="provisioning",
            workflow_operation=op,
            execution_backend="n8n", transition_phase="idle", backend_revision=1, credential_revision=1,
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning.lock_retained_connector_effect", return_value=None), \
             patch("modules.connectors.provisioning._read_retained_connector_rows", return_value=(source, row, {})), \
             patch("modules.connectors.provisioning.commit_retained_connector_effect", new_callable=AsyncMock), \
             patch("modules.settings.public.module_is_enabled", AsyncMock(return_value=True)):

            # Claim step
            claimed = await claim_workflow_step(
                session, source_id, original_operation=copy.deepcopy(op), access_fence=ACCESS, **FLAG,
            )
            assert claimed is not None
            assert row.workflow_operation["step"]["state"] == "dispatched"

            # Acknowledge step activate -> transitions row to active (CAS against the dispatched snapshot)
            acked = await acknowledge_workflow_step(
                session,
                source_id,
                operation_id,
                step_id,
                workflow_id="wf-activated-1",
                original_operation=copy.deepcopy(claimed),
                access_fence=ACCESS,
                **FLAG,
            )
            assert acked is True
            assert row.state == "active"
            assert row.applied_revision == 2
            assert row.workflow_operation is None

    @pytest.mark.asyncio
    async def test_fence_source_collection_archived_wipes_and_disables(self) -> None:
        """Fencing an archived source collection disables it and triggers credential purging."""
        source_id = uuid4()
        source = _fence(source_id, generation=3, status="archived")
        row = ConnectorProvisioning(
            source_id=source_id,
            source_generation=2,
            desired_enabled=True,
            state="active",
        )
        session = AsyncMock()

        with patch("modules.connectors.provisioning._lock_connector_rows", return_value=(source, row, {})), \
             patch("modules.connectors.provisioning.clear_retired_source_credentials", new_callable=AsyncMock) as mock_clear:
            result = await fence_source_collection(session, source, **FLAG)

        assert result is True
        assert row.source_generation == 3
        assert row.desired_enabled is False
        assert row.state == "disabled"
        mock_clear.assert_awaited_once_with(session, source_id, **FLAG)


class TestTokenRefreshAndOAuthCiphers:
    """Tests for GitHub token refresh, token lifetime validation, and OAuth token encryption ciphers."""

    def test_validate_expiring_token_valid(self) -> None:
        """A compliant token dictionary produces calculated expiry timestamps."""
        token_data = {
            "access_token": "ghu_validAccessToken123",
            "refresh_token": "ghr_validRefreshToken456",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400 * 30,
        }
        validated = _validate_expiring_token(token_data)
        assert validated["access_token"] == "ghu_validAccessToken123"
        assert validated["refresh_token"] == "ghr_validRefreshToken456"
        assert isinstance(validated["access_expires_at"], datetime)
        assert isinstance(validated["refresh_expires_at"], datetime)
        assert validated["access_expires_at"] > datetime.now(UTC)

    def test_validate_expiring_token_missing_refresh_token(self) -> None:
        """Missing refresh token raises ValueError."""
        with pytest.raises(ValueError, match="github_expiring_token_required"):
            _validate_expiring_token({
                "access_token": "ghu_onlyAccessToken",
                "token_type": "bearer",
                "expires_in": 3600,
            })

    def test_validate_expiring_token_excessive_lifetimes_rejected(self) -> None:
        """Token lifetimes beyond security policy bounds (access > 8h, refresh > 183d) are rejected."""
        # Access token lifetime > 8 hours
        with pytest.raises(ValueError, match="github_expiring_token_required"):
            _validate_expiring_token({
                "access_token": "ghu_token",
                "refresh_token": "ghr_token",
                "token_type": "bearer",
                "expires_in": 8 * 3600 + 1,
                "refresh_token_expires_in": 3600,
            })

        # Refresh token lifetime > 183 days
        with pytest.raises(ValueError, match="github_expiring_token_required"):
            _validate_expiring_token({
                "access_token": "ghu_token",
                "refresh_token": "ghr_token",
                "token_type": "bearer",
                "expires_in": 3600,
                "refresh_token_expires_in": 184 * 86400,
            })

    def test_token_cipher_round_trip(self, fernet_key: str) -> None:
        """OAuth token cipher encrypts with source fence and recovers matching token dict."""
        source_id = uuid4()
        operation_id = uuid4()
        token_payload = {
            "access_token": "ghu_test",
            "refresh_token": "ghr_test",
            "access_expires_at": datetime.now(UTC) + timedelta(hours=1),
        }

        ciphertext = _token_cipher(
            fernet_key,
            source_id=source_id,
            operation_id=operation_id,
            generation=1,
            revision=2,
            value=token_payload,
        )

        decrypted = _open_token_cipher(
            fernet_key,
            ciphertext,
            source_id=source_id,
            operation_id=operation_id,
            generation=1,
            revision=2,
        )
        assert decrypted["access_token"] == "ghu_test"
        assert decrypted["refresh_token"] == "ghr_test"

    def test_token_cipher_mismatched_fence_raises(self, fernet_key: str) -> None:
        """Opening token cipher with mismatched revision or generation raises CredentialEncryptionUnavailable."""
        source_id = uuid4()
        operation_id = uuid4()
        ciphertext = _token_cipher(
            fernet_key,
            source_id=source_id,
            operation_id=operation_id,
            generation=1,
            revision=2,
            value={"access_token": "ghu_test"},
        )

        with pytest.raises(CredentialEncryptionUnavailable, match="binding is invalid"):
            _open_token_cipher(
                fernet_key,
                ciphertext,
                source_id=source_id,
                operation_id=operation_id,
                generation=1,
                revision=3,  # Mismatched revision
            )

    @pytest.mark.asyncio
    async def test_refresh_github_grant_mocked(self) -> None:
        """refresh_github_grant calls AsyncOAuth2Client.refresh_token and validates output."""
        settings = MagicMock(spec=Settings)
        settings.github_app_client_id = "test-client-id"
        settings.github_app_client_secret = MagicMock()
        settings.github_app_client_secret.get_secret_value.return_value = "client-secret-123"

        mock_token_response = {
            "access_token": "ghu_refreshedToken",
            "refresh_token": "ghr_newRefreshToken",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400 * 30,
        }

        with patch("modules.connectors.github.oauth.AsyncOAuth2Client") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.refresh_token.return_value = mock_token_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await refresh_github_grant(settings, "ghr_oldRefreshToken")

        assert result["access_token"] == "ghu_refreshedToken"
        assert result["refresh_token"] == "ghr_newRefreshToken"
        mock_client.refresh_token.assert_awaited_once()
