import asyncio
import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from alembic.ddl.postgresql import PostgresqlImpl
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import modules  # noqa: F401  # Domain model packages are imported here as they are added.
from core.auth.models import AuthSession, GoogleIdentity, Owner
from core.database import Base
from core.demo_seed import DemoSeedReceipt
from core.realtime import ReplayHead, ReplayRecord
from core.remote_heavy_models import RemoteHeavyGuard
from core.workspaces.models import Workspace, WorkspaceInvitation, WorkspaceMembership
from modules.agents.models import AgentApproval, AgentEffect, AgentRun, AgentToolCall
from modules.automations.models import (
    Automation,
    AutomationCursor,
    AutomationRevision,
    AutomationRun,
    AutomationRunAction,
    AutomationSchedule,
    AutomationTrigger,
)
from modules.backup.models import BackupActivity, BackupControl, BackupOperation
from modules.chat.models import AgentActivityLink, Conversation, Message, ResponseRun, StreamEvent
from modules.connectors.models import (
    AgentBrowserGrant,
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorProvisioning,
    ConnectorWorldCredential,  # noqa: F401  # registers table metadata
    GithubOAuthOperation,
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.dashboard.models import (
    BriefSchedule,
    DailyBrief,
    Dashboard,
    DashboardGroup,
    DashboardLayout,
    GadgetDefinition,
    GadgetHighlightProgress,
    GadgetInstance,
    GadgetPlacement,
)
from modules.goals.models import Goal
from modules.ingestion.models import (
    CollectorCredential,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
from modules.knowledge.documents.models import (
    Document,
    DocumentChunk,
    DocumentCleanupOperation,  # noqa: F401  # registers table metadata
    DocumentVersion,
    NormalizedDocumentIdentity,
    NormalizedVersionProvenance,
)
from modules.knowledge.entities.models import (
    Entity,
    EntityAlias,
    EntityAliasEvidence,
    EntityCorrectionDecision,
    EntityEvidenceMembership,
    EntityExtractionResult,
    EntityExtractionWork,
    EntityFieldEvidence,
    EntityOwnerAction,
    EntityRedirect,
)
from modules.knowledge.observations.models import (
    Observation,  # noqa: F401  # registers table metadata
)
from modules.knowledge.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipSnapshotHistory,
)
from modules.knowledge.temporal.models import (
    GraphAllocation,
    GraphChange,
    GraphDispatch,
    GraphMapping,
    GraphOperation,
    GraphPartition,
    GraphRebuildDependency,
    GraphReceipt,
    GraphReconcileMember,
    GraphReconcileRun,
    GraphSupport,
)
from modules.memory.models import Memory, MemoryCandidate, MemoryPrivacyRecord
from modules.news.models import (
    NewsObservation,
    NewsRecoveryCheckpoint,
    NewsStory,
    NewsStoryIdentity,
)
from modules.news.topics import Topic
from modules.notifications.models import Notification
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.settings.models import AISettingsRecord, OnboardingStateRecord, OwnerPreferencesRecord
from modules.sources.models import Source, SourcePurgeOperation
from modules.tasks.models import Task
from modules.timeline.models import (
    Event,
    EventAudit,
    EventEvidence,
    EventParticipant,
    EventSuppression,
    ParticipantEvidence,
    TimelineExtractionResult,
    TimelineExtractionWork,
)
from modules.tools.models import BrowserPageEvidence, BrowserReadJob
import modules.translations.models  # noqa: F401

_auth_models = (AuthSession, GoogleIdentity, Owner)
_workspace_models = (Workspace, WorkspaceMembership, WorkspaceInvitation)
_demo_seed_models = (DemoSeedReceipt,)
_core_remote_heavy_models = (RemoteHeavyGuard,)
_chat_models = (Conversation, Message, ResponseRun, StreamEvent, AgentActivityLink)
_memory_models = (Memory, MemoryCandidate, MemoryPrivacyRecord)
_browser_read_models = (BrowserReadJob, BrowserPageEvidence)
_agent_models = (AgentRun, AgentToolCall, AgentApproval, AgentEffect)
_automation_models = (
    Automation, AutomationRevision, AutomationTrigger, AutomationSchedule, AutomationRun, AutomationRunAction,
    AutomationCursor,
)
_backup_models = (BackupControl, BackupOperation, BackupActivity)
_library_models = (

    Source, SourcePurgeOperation, Document, DocumentVersion, DocumentChunk,
    NormalizedDocumentIdentity, NormalizedVersionProvenance,
)
_ingestion_models = (
    CollectorCredential,
    EventOutbox,
    IngestionBatch,
    IngestionRun,
    IngestionStage,
    ObservationNormalization,
    SourceIngestionState,
    SourceObservation,
)
_search_models = (IndexGeneration, SearchIndexItem)
_connector_models = (
    AgentBrowserGrant, ConnectorProvisioning, ConnectorManagedCredential, ConnectorNativeCredential, GithubOAuthOperation,
    GithubWebhookCapacity, GithubWebhookDelivery, GithubWebhookOutbox, GithubSourceHint,
)
_realtime_models = (ReplayHead, ReplayRecord)
_dashboard_models = (
    Dashboard,
    DashboardGroup,
    GadgetDefinition,
    GadgetHighlightProgress,
    GadgetInstance,
    DashboardLayout,
    GadgetPlacement,
)
_task_goal_models = (Task, Goal, Topic)
_brief_models = (DailyBrief, BriefSchedule, Notification)
_news_models = (NewsStory, NewsStoryIdentity, NewsObservation, NewsRecoveryCheckpoint)
_knowledge_models = (
    Entity,
    EntityAlias,
    EntityEvidenceMembership,
    EntityAliasEvidence,
    EntityFieldEvidence,
    EntityOwnerAction,
    EntityRedirect,
    EntityCorrectionDecision,
    EntityExtractionWork,
    EntityExtractionResult,
    Relationship,
    RelationshipEvidence,
    RelationshipSnapshotHistory,
    Event,
    EventParticipant,
    EventEvidence,
    ParticipantEvidence,
    EventAudit,
    EventSuppression,
    TimelineExtractionWork,
    TimelineExtractionResult,
)
_temporal_models = (
    GraphAllocation, GraphPartition, GraphMapping, GraphSupport, GraphOperation,
    GraphReceipt, GraphChange, GraphReconcileRun, GraphDispatch, GraphReconcileMember, GraphRebuildDependency,
)
_settings_models = (AISettingsRecord, OnboardingStateRecord, OwnerPreferencesRecord)


class _WideVersionImpl(PostgresqlImpl):
    """Several shipped revision ids exceed Alembic's default VARCHAR(32) version column (online and --sql)."""

    __dialect__ = "postgresql"

    def version_table_impl(self, **kw):  # type: ignore[no-untyped-def]
        table = super().version_table_impl(**kw)
        table.c.version_num.type = sa.String(255)
        return table


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Configure Alembic SQL generation without opening a database connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the Alembic migration context against the supplied database connection."""
    # Widen only databases created before the wide version table (read-only commands stay DDL-free).
    width = connection.execute(text(
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'alembic_version' AND column_name = 'version_num'"
    )).scalar()
    if width is not None and width < 255:
        connection.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))
    connection.commit()
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async migration engine and execute Alembic through its synchronous migration callback."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations using the configured online async database connection."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
