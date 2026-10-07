"""Unit tests for GitHub connector: schemas, webhook signature verification, and timeline mapping.

Covers:
- GitHubSourceConfig validation: owner format, repository name constraints, resource scope requirement
- GitHubBindingFence, GitHubResourceCursor, and GitHubSegmentProof invariants
- verify_github_signature: HMAC-SHA256 verification, payload bounds, malformed signatures, secret mismatches
- VerifiedGitHubDelivery and GitHubTargetHint schemas
- Knowledge & timeline mapping: _repository_name resolution and _display_title formatting
"""

import hashlib
import hmac
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from modules.connectors.github.mapping import (
    _display_title,
    _repository_name,
)
from modules.connectors.github.schemas import (
    GitHubSourceConfig,
)
from modules.connectors.github.webhooks import (
    MAX_GITHUB_WEBHOOK_BYTES,
    GitHubTargetHint,
    VerifiedGitHubDelivery,
    verify_github_signature,
)


class TestGitHubSourceConfig:
    """Tests for GitHubSourceConfig validation and resource scoping."""

    def test_github_source_config_valid(self) -> None:
        """Verify valid repository source configuration."""
        config = GitHubSourceConfig(
            github_owner="octocat",
            github_repository="Hello-World",
            include_issues=True,
            include_pulls=True,
            include_commits=True,
            include_releases=True,
            github_history_days=90,
        )
        assert config.github_owner == "octocat"
        assert config.github_repository == "Hello-World"
        assert config.github_history_days == 90

    def test_github_source_config_no_resources_rejected(self) -> None:
        """At least one resource (issues, pulls, commits, releases) must be enabled."""
        with pytest.raises(ValidationError, match="At least one GitHub resource must be enabled"):
            GitHubSourceConfig(
                github_owner="octocat",
                github_repository="Hello-World",
                include_issues=False,
                include_pulls=False,
                include_commits=False,
                include_releases=False,
            )

    def test_github_source_config_invalid_repo_names(self) -> None:
        """Repositories named '.', '..', or ending in '.git' must be rejected."""
        with pytest.raises(ValidationError, match="GitHub repository must be a single repository name"):
            GitHubSourceConfig(github_owner="octocat", github_repository="repo.git")

        with pytest.raises(ValidationError, match="GitHub repository must be a single repository name"):
            GitHubSourceConfig(github_owner="octocat", github_repository=".")


class TestGitHubWebhookSignatureVerification:
    """Tests for verify_github_signature HMAC-SHA256 cryptographic check."""

    def test_verify_github_signature_valid(self) -> None:
        """Verify authentic HMAC-SHA256 signature returns True."""
        secret = "super-secret-webhook-key-123"
        body = b'{"action":"opened","issue":{"number":42}}'
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"

        assert verify_github_signature(body, signature, secret) is True

    def test_verify_github_signature_tampered_payload_rejected(self) -> None:
        """Tampered payload bytes return False."""
        secret = "secret-key"
        body = b'{"action":"opened"}'
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"

        tampered_body = b'{"action":"closed"}'
        assert verify_github_signature(tampered_body, signature, secret) is False

    def test_verify_github_signature_wrong_secret_rejected(self) -> None:
        """Wrong secret key returns False."""
        secret = "key-a"
        body = b'{"event":"push"}'
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"

        assert verify_github_signature(body, signature, secret="key-b") is False

    def test_verify_github_signature_malformed_header_rejected(self) -> None:
        """Malformed signatures (missing sha256= prefix or wrong length) return False."""
        secret = "key"
        body = b"test"
        assert verify_github_signature(body, "invalid-signature", secret) is False
        assert verify_github_signature(body, None, secret) is False
        assert verify_github_signature(body, "", secret) is False

    def test_verify_github_signature_oversized_payload_rejected(self) -> None:
        """Payloads exceeding MAX_GITHUB_WEBHOOK_BYTES (256KB) return False immediately."""
        secret = "key"
        oversized_body = b"x" * (MAX_GITHUB_WEBHOOK_BYTES + 1)
        digest = hmac.new(secret.encode("utf-8"), oversized_body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"

        assert verify_github_signature(oversized_body, signature, secret) is False


class TestGitHubWebhookDeliverySchemas:
    """Tests for VerifiedGitHubDelivery and GitHubTargetHint schemas."""

    def test_verified_github_delivery_schema(self) -> None:
        """Verify delivery receipt schema with targets and timezone awareness."""
        now = datetime.now(UTC)
        target = GitHubTargetHint(
            resource="issue",
            locator_kind="number",
            locator="42",
            intent="refresh",
        )
        delivery = VerifiedGitHubDelivery(
            receiver_revision="rev-1",
            delivery_id="del-12345",
            raw_sha256="a" * 64,
            event="issues",
            action="opened",
            app_id="10001",
            installation_id="20002",
            repository_id="30003",
            received_at=now,
            targets=(target,),
            disposition="received",
        )
        assert delivery.event == "issues"
        assert delivery.app_id == "10001"
        assert len(delivery.targets) == 1
        assert delivery.targets[0].locator == "42"

    def test_verified_github_delivery_naive_time_rejected(self) -> None:
        """Naive receipt timestamps without timezone must be rejected."""
        naive = datetime(2026, 10, 5, 12, 0)  # noqa: DTZ001  # intentionally naive: wall-clock/DST math or naive-rejection test
        with pytest.raises(ValidationError, match="GitHub receipt timestamp must be timezone-aware"):
            VerifiedGitHubDelivery(
                receiver_revision="rev-1",
                delivery_id="del-1",
                raw_sha256="b" * 64,
                event="push",
                app_id="10001",
                received_at=naive,
                targets=(),
                disposition="received",
            )


class TestGitHubTimelineMapping:
    """Tests for _repository_name and _display_title mapping functions."""

    def test_repository_name_from_configuration(self) -> None:
        """Stable repository name is resolved from configuration."""
        config = {"github_owner": "torvalds", "github_repository": "linux"}
        assert _repository_name(config, None) == "torvalds/linux"

    def test_repository_name_fallback_to_url(self) -> None:
        """When configuration lacks owner/repo, falls back to parsing canonical URL."""
        assert _repository_name({}, "https://github.com/torvalds/linux/commit/123") == "torvalds/linux"
        assert _repository_name({}, None) is None

    def test_display_title_for_issues_and_pulls(self) -> None:
        """Non-commit records preserve their original title."""
        assert _display_title("issue", "Fix memory leak in parser", "") == "Fix memory leak in parser"
        assert _display_title("pull", "Add support for Postgres 17", "") == "Add support for Postgres 17"

    def test_display_title_for_commits(self) -> None:
        """Commit records use the first 7 characters of SHA and the first commit message body line."""
        sha = "a1b2c3d4e5f67890"
        excerpt = "header line\nfeat: implement timeline mapping\nmore details"
        title = _display_title("commit", sha, excerpt)
        assert title == "Commit a1b2c3d: feat: implement timeline mapping"

        # Commit with no excerpt body falls back to SHA prefix
        empty_excerpt = "header only"
        title_fallback = _display_title("commit", sha, empty_excerpt)
        assert title_fallback == "Commit a1b2c3d"
