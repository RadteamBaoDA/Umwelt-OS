import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import modules  # noqa: F401  # Domain model packages are imported here as they are added.
from core.auth.models import AuthSession, Owner
from core.remote_heavy_models import RemoteHeavyGuard
from core.database import Base
from modules.knowledge.documents.models import (
    Document, DocumentChunk, DocumentVersion, NormalizedDocumentIdentity, NormalizedVersionProvenance,
)
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
from modules.connectors.models import AgentBrowserGrant, ConnectorManagedCredential, ConnectorProvisioning
from modules.sources.models import Source, SourcePurgeOperation
from modules.search.models import IndexGeneration, SearchIndexItem
from modules.knowledge.entities.models import (
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
)
from modules.knowledge.relationships.models import Relationship, RelationshipEvidence, RelationshipSnapshotHistory
from modules.knowledge.temporal.models import (
    GraphAllocation, GraphPartition, GraphMapping, GraphSupport, GraphOperation,
    GraphReceipt, GraphChange, GraphReconcileRun, GraphDispatch, GraphReconcileMember, GraphRebuildDependency,
)
from modules.timeline.models import Event, EventParticipant, EventEvidence, ParticipantEvidence, EventAudit, EventSuppression, TimelineExtractionWork, TimelineExtractionResult
from modules.settings.models import AISettingsRecord, OwnerPreferencesRecord
from core.realtime import ReplayHead, ReplayRecord
from modules.chat.models import AgentActivityLink, Conversation, Message, ResponseRun, StreamEvent
from modules.memory.models import Memory, MemoryCandidate, MemoryPrivacyRecord
from modules.tools.models import BrowserPageEvidence, BrowserReadJob
from modules.agents.models import AgentApproval, AgentEffect, AgentRun, AgentToolCall

_auth_models = (AuthSession, Owner)
_core_remote_heavy_models = (RemoteHeavyGuard,)
_chat_models = (Conversation, Message, ResponseRun, StreamEvent, AgentActivityLink)
_memory_models = (Memory, MemoryCandidate, MemoryPrivacyRecord)
_browser_read_models = (BrowserReadJob, BrowserPageEvidence)
_agent_models = (AgentRun, AgentToolCall, AgentApproval, AgentEffect)
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
_connector_models = (AgentBrowserGrant, ConnectorProvisioning, ConnectorManagedCredential)
_realtime_models = (ReplayHead, ReplayRecord)
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
