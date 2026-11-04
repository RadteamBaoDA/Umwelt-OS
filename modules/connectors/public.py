"""Public Connectors contract (facade).

Implementation lives in ``modules.connectors.service.*``; every public name is re-exported here
so callers keep importing from ``modules.connectors.public``.

Testing note: monkeypatch a helper on the ``service.*`` module that defines and uses it. Patching
the same name on this facade only affects callers that resolve it through the facade at call time
and silently does nothing for helpers used inside ``service.*``.
"""

# ruff: noqa: F401, PLC0414
import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from ipaddress import ip_address
from socket import getaddrinfo
from typing import TYPE_CHECKING, Any, Literal, TypeGuard
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.async_runtime import run_dns_lookup
from core.errors import (
    BadRequestError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnavailableError,
    ValidationFailedError,
)
from core.ids import uuid7
from core.workspaces import public as workspaces
from core.workspaces.schemas import AccessFence, InternalJobScope, Scope, WorkspaceContext
from modules.connectors.backends import (
    NATIVE_PROVIDERS,
    PROVIDER_SCOPE_FIELDS,
    native_dispatch_supported,
)
from modules.connectors.backends import (
    is_native_provider as _is_native_provider,
)
from modules.connectors.data_shapes import (
    DataShape as DataShape,
)
from modules.connectors.data_shapes import (
    source_data_shapes as source_data_shapes,
)
from modules.connectors.live_map_catalog import (
    CATALOG_IDS as CATALOG_IDS,
)
from modules.connectors.live_map_catalog import (
    live_terms_catalog as live_terms_catalog,
)
from modules.connectors.metric_catalog import (
    MetricInfo as MetricInfo,
)
from modules.connectors.metric_catalog import (
    ProviderMetrics as ProviderMetrics,
)
from modules.connectors.metric_catalog import (
    metric_catalog as metric_catalog,
)
from modules.connectors.metric_catalog import (
    provider_metrics as provider_metrics,
)
from modules.connectors.models import (
    AgentBrowserGrant,
    ConnectorManagedCredential,
    ConnectorNativeCredential,
    ConnectorOAuthGrant,
    ConnectorProvisioning,
    ConnectorRestCredential,
    ConnectorWorldCredential,
    GithubOAuthGrant,
    GithubSourceHint,
    GithubWebhookCapacity,
    GithubWebhookDelivery,
    GithubWebhookOutbox,
)
from modules.connectors.provider_options import (
    ISO_COUNTRY_PATTERN,
    MAX_COINS,
    MAX_JOB_KEYWORDS,
    MAX_VN_SYMBOLS,
    CoinId,
    CryptoPair,
    EonetCategory,
    EonetStatus,
    FxCurrency,
    GdacsAlert,
    GdacsType,
    GdeltCountry,
    GdeltLang,
    GdeltTimespan,
    HnList,
    IsoCountry,
    JobCity,
    JobKeyword,
    JobLevel,
    NvdSeverity,
    NwsArea,
    NwsSeverity,
    OecdArea,
    Region,
    SatnogsStatus,
    UsgsMagnitude,
    UsgsPeriod,
    VnSymbol,
    WbIndicator,
    WikiLanguage,
    clean_query,
    option_fields,
)
from modules.connectors.provider_specs import (
    BUILTIN_PROVIDERS as BUILTIN_PROVIDERS,
)
from modules.connectors.provider_specs import (
    COLLECTION_FILTER_FIELDS,
    INCREMENTAL_PROVIDERS,
    min_interval_minutes,
)
from modules.connectors.providers.cii import CiiProjection
from modules.connectors.sections import MAX_SECTIONS, SECTION_CATALOG
from modules.ingestion.schemas import IngestionRecord, TelegramRawDelivery
from modules.sources.schemas import ConnectorSource, SourceFence

if TYPE_CHECKING:
    from core.config import Settings
    from modules.connectors.collection_schemas import (
        CollectionAdmissionRead as CollectionAdmissionRead,
    )
    from modules.connectors.collection_schemas import (
        CollectionAdmissionRequest as CollectionAdmissionRequest,
    )
    from modules.connectors.collection_schemas import (
        CollectionRequestRead as CollectionRequestRead,
    )
    from modules.connectors.collection_schemas import (
        CollectionRequestRef as CollectionRequestRef,
    )
    from modules.connectors.github.schemas import (
        GitHubBindingFence as GitHubBindingFence,
    )
    from modules.connectors.github.schemas import (
        GitHubSegmentProof as GitHubSegmentProof,
    )
    from modules.connectors.github.sync import GitHubValidatedSegment
    from modules.connectors.github.webhooks import (
        GitHubTargetHint,
        GitHubWebhookReceipt,
        VerifiedGitHubDelivery,
    )
from modules.connectors.service.access import (
    _connector_access as _connector_access,
)
from modules.connectors.service.access import (
    _connector_actor as _connector_actor,
)
from modules.connectors.service.access import (
    _read_scoped_source as _read_scoped_source,
)
from modules.connectors.service.access import (
    observability_queue_summary as observability_queue_summary,
)
from modules.connectors.service.collection_requests import (
    admit_managed_collection as admit_managed_collection,
)
from modules.connectors.service.collection_requests import (
    collection_timing as collection_timing,
)
from modules.connectors.service.collection_requests import (
    default_retention_days as default_retention_days,
)
from modules.connectors.service.collection_requests import (
    get_collection_request as get_collection_request,
)
from modules.connectors.service.collection_requests import (
    lock_collection_request_in_uow as lock_collection_request_in_uow,
)
from modules.connectors.service.collection_requests import (
    map_github_version as map_github_version,
)
from modules.connectors.service.collection_requests import (
    overlap_floor as overlap_floor,
)
from modules.connectors.service.collection_requests import (
    purge_collection_history as purge_collection_history,
)
from modules.connectors.service.collection_requests import (
    recoverable_collection_request_ids as recoverable_collection_request_ids,
)
from modules.connectors.service.collection_requests import (
    request_collection as request_collection,
)
from modules.connectors.service.collection_requests import (
    settle_collection_exit as settle_collection_exit,
)
from modules.connectors.service.collection_requests import (
    settle_collection_in_uow as settle_collection_in_uow,
)
from modules.connectors.service.collection_requests import (
    validate_public_url as validate_public_url,
)
from modules.connectors.service.config import (
    _NATIVE_ONLY_DEFAULTS as _NATIVE_ONLY_DEFAULTS,
)
from modules.connectors.service.config import (
    _UNSET as _UNSET,
)
from modules.connectors.service.config import (
    allow_external_collector_credential_issue as allow_external_collector_credential_issue,
)
from modules.connectors.service.config import (
    collection_allowed as collection_allowed,
)
from modules.connectors.service.config import (
    default_schedule_interval_minutes as default_schedule_interval_minutes,
)
from modules.connectors.service.config import (
    fence_source_collection as fence_source_collection,
)
from modules.connectors.service.config import (
    get_connector_configuration as get_connector_configuration,
)
from modules.connectors.service.config import (
    get_native_credential_snapshot as get_native_credential_snapshot,
)
from modules.connectors.service.config import (
    require_batch_fence as require_batch_fence,
)
from modules.connectors.service.config import (
    require_collection_fence as require_collection_fence,
)
from modules.connectors.service.config import (
    save_connector_configuration as save_connector_configuration,
)
from modules.connectors.service.config import (
    serialize_source_configuration as serialize_source_configuration,
)
from modules.connectors.service.config import (
    validate_provider_credential_revision_in_uow as validate_provider_credential_revision_in_uow,
)
from modules.connectors.service.config import (
    validate_world_credential_operation_in_uow as validate_world_credential_operation_in_uow,
)
from modules.connectors.service.github_binding import (
    _GITHUB_PAUSE_STATES as _GITHUB_PAUSE_STATES,
)
from modules.connectors.service.github_binding import (
    GitHubHintClaim as GitHubHintClaim,
)
from modules.connectors.service.github_binding import (
    _claim_matches_binding as _claim_matches_binding,
)
from modules.connectors.service.github_binding import (
    _current_github_binding_in_uow as _current_github_binding_in_uow,
)
from modules.connectors.service.github_binding import (
    _github_binding_from_rows as _github_binding_from_rows,
)
from modules.connectors.service.github_binding import (
    _pause_prepared_github_hints as _pause_prepared_github_hints,
)
from modules.connectors.service.github_binding import (
    _validate_source_lifecycle_cleanup_rows as _validate_source_lifecycle_cleanup_rows,
)
from modules.connectors.service.github_binding import (
    _validated_github_segment as _validated_github_segment,
)
from modules.connectors.service.github_binding import (
    apply_source_lifecycle_cleanup_in_uow as apply_source_lifecycle_cleanup_in_uow,
)
from modules.connectors.service.github_binding import (
    apply_source_pause_cleanup_in_uow as apply_source_pause_cleanup_in_uow,
)
from modules.connectors.service.github_binding import (
    get_github_binding_fence as get_github_binding_fence,
)
from modules.connectors.service.github_binding import (
    lock_github_binding_fence_in_uow as lock_github_binding_fence_in_uow,
)
from modules.connectors.service.github_binding import (
    lock_github_hints_for_visibility_pause_in_uow as lock_github_hints_for_visibility_pause_in_uow,
)
from modules.connectors.service.github_binding import (
    prepare_source_lifecycle_cleanup_in_uow as prepare_source_lifecycle_cleanup_in_uow,
)
from modules.connectors.service.github_binding import (
    prepare_source_pause_cleanup_in_uow as prepare_source_pause_cleanup_in_uow,
)
from modules.connectors.service.github_binding import (
    reset_github_collection_cursor as reset_github_collection_cursor,
)
from modules.connectors.service.github_binding import (
    validate_github_collection_segment as validate_github_collection_segment,
)
from modules.connectors.service.github_binding import (
    validate_github_collection_segment_in_uow as validate_github_collection_segment_in_uow,
)
from modules.connectors.service.github_hints import (
    GitHubEventBinding as GitHubEventBinding,
)
from modules.connectors.service.github_hints import (
    GitHubEventBindingPage as GitHubEventBindingPage,
)
from modules.connectors.service.github_hints import (
    GitHubHintEnqueueResult as GitHubHintEnqueueResult,
)
from modules.connectors.service.github_hints import (
    PackagedCollectionWakeResult as PackagedCollectionWakeResult,
)
from modules.connectors.service.github_hints import (
    _acknowledge_prepared_github_hint as _acknowledge_prepared_github_hint,
)
from modules.connectors.service.github_hints import (
    _append_github_fanout_admission as _append_github_fanout_admission,
)
from modules.connectors.service.github_hints import (
    _github_target_digest as _github_target_digest,
)
from modules.connectors.service.github_hints import (
    _GitHubFanoutAdmission as _GitHubFanoutAdmission,
)
from modules.connectors.service.github_hints import (
    _GitHubFanoutBinding as _GitHubFanoutBinding,
)
from modules.connectors.service.github_hints import (
    _GitHubFanoutPage as _GitHubFanoutPage,
)
from modules.connectors.service.github_hints import (
    _reserve_github_hint_slot as _reserve_github_hint_slot,
)
from modules.connectors.service.github_hints import (
    _store_github_fanout_admission as _store_github_fanout_admission,
)
from modules.connectors.service.github_hints import (
    _validated_github_fanout_page as _validated_github_fanout_page,
)
from modules.connectors.service.github_hints import (
    acknowledge_github_hint as acknowledge_github_hint,
)
from modules.connectors.service.github_hints import (
    acknowledge_github_hint_in_uow as acknowledge_github_hint_in_uow,
)
from modules.connectors.service.github_hints import (
    claim_github_hint as claim_github_hint,
)
from modules.connectors.service.github_hints import (
    enqueue_github_hint as enqueue_github_hint,
)
from modules.connectors.service.github_hints import (
    list_github_event_bindings as list_github_event_bindings,
)
from modules.connectors.service.github_hints import (
    persist_verified_github_delivery as persist_verified_github_delivery,
)
from modules.connectors.service.github_hints import (
    reconcile_github_source_hints_lifecycle as reconcile_github_source_hints_lifecycle,
)
from modules.connectors.service.packaged import (
    _post_wake as _post_wake,
)
from modules.connectors.service.packaged import (
    n8n_callback_base as n8n_callback_base,
)
from modules.connectors.service.packaged import (
    wake_packaged_collection as wake_packaged_collection,
)
from modules.connectors.service.schemas import (
    DEFAULT_OVERLAP as DEFAULT_OVERLAP,
)
from modules.connectors.service.schemas import (
    DEFAULT_TIMEZONE as DEFAULT_TIMEZONE,
)
from modules.connectors.service.schemas import (
    CoinGeckoAsset as CoinGeckoAsset,
)
from modules.connectors.service.schemas import (
    CollectionFence as CollectionFence,
)
from modules.connectors.service.schemas import (
    ConnectorConfig as ConnectorConfig,
)
from modules.connectors.service.schemas import (
    ConnectorConfigurationRequest as ConnectorConfigurationRequest,
)
from modules.connectors.service.schemas import (
    ConnectorConfigurationSnapshot as ConnectorConfigurationSnapshot,
)
from modules.connectors.service.schemas import (
    ConnectorPreview as ConnectorPreview,
)
from modules.connectors.service.schemas import (
    ConnectorReceipt as ConnectorReceipt,
)
from modules.connectors.service.schemas import (
    ConnectorRecord as ConnectorRecord,
)
from modules.connectors.service.schemas import (
    CrawlRequest as CrawlRequest,
)
from modules.connectors.service.schemas import (
    CrawlResult as CrawlResult,
)
from modules.connectors.service.schemas import (
    NativeCredentialRevocationSnapshot as NativeCredentialRevocationSnapshot,
)
from modules.connectors.service.schemas import (
    NativeCredentialSnapshot as NativeCredentialSnapshot,
)
from modules.connectors.service.schemas import (
    NoChangeRequest as NoChangeRequest,
)
from modules.connectors.service.schemas import (
    ProviderCollectionPage as ProviderCollectionPage,
)
from modules.connectors.service.schemas import (
    ProviderRateLimited as ProviderRateLimited,
)
from modules.connectors.service.schemas import (
    ProviderScopeSnapshot as ProviderScopeSnapshot,
)
from modules.connectors.service.schemas import (
    RSSRequest as RSSRequest,
)
from modules.connectors.service.schemas import (
    TelegramScopeValidation as TelegramScopeValidation,
)
from modules.connectors.service.schemas import (
    TelegramUpdatePage as TelegramUpdatePage,
)
from modules.connectors.service.schemas import (
    is_native_provider as is_native_provider,
)
from modules.connectors.service.scope import (
    _ALTERNATIVE_SCOPE_FIELDS as _ALTERNATIVE_SCOPE_FIELDS,
)
from modules.connectors.service.scope import (
    OPTIONAL_SCOPE_FIELDS as OPTIONAL_SCOPE_FIELDS,
)
from modules.connectors.service.scope import (
    AgentBrowserGrantPatch as AgentBrowserGrantPatch,
)
from modules.connectors.service.scope import (
    AgentBrowserScope as AgentBrowserScope,
)
from modules.connectors.service.scope import (
    AgentBrowserScopeRead as AgentBrowserScopeRead,
)
from modules.connectors.service.scope import (
    _agent_browser_scope_url as _agent_browser_scope_url,
)
from modules.connectors.service.scope import (
    _scope_hash as _scope_hash,
)
from modules.connectors.service.scope import (
    _scope_values as _scope_values,
)
from modules.connectors.service.scope import (
    agent_browser_target_in_scope as agent_browser_target_in_scope,
)
from modules.connectors.service.scope import (
    cii_v8_availability as cii_v8_availability,
)
from modules.connectors.service.scope import (
    export_provider_scope as export_provider_scope,
)
from modules.connectors.service.scope import (
    get_current_provider_scope as get_current_provider_scope,
)
from modules.connectors.service.scope import (
    invalidate_agent_browser_grant_in_uow as invalidate_agent_browser_grant_in_uow,
)
from modules.connectors.service.scope import (
    resolve_agent_browser_scope as resolve_agent_browser_scope,
)
from modules.connectors.service.scope import (
    update_agent_browser_grant_in_uow as update_agent_browser_grant_in_uow,
)

# DTOs other modules import from ``collection_schemas`` / ``github.schemas``. ``collection_schemas``
# imports this facade, so they are resolved lazily (PEP 562) to avoid an import cycle; type
# checkers see them through the ``TYPE_CHECKING`` re-exports above.
_LAZY_DTO_MODULES = {
    "CollectionAdmissionRead": "modules.connectors.collection_schemas",
    "CollectionAdmissionRequest": "modules.connectors.collection_schemas",
    "CollectionRequestRead": "modules.connectors.collection_schemas",
    "CollectionRequestRef": "modules.connectors.collection_schemas",
    "ManagedConnectorReceipt": "modules.connectors.collection_schemas",
    "ManagedCrawlRequest": "modules.connectors.collection_schemas",
    "ManagedNoChanges": "modules.connectors.collection_schemas",
    "GitHubBindingFence": "modules.connectors.github.schemas",
    "GitHubSegmentProof": "modules.connectors.github.schemas",
}


def __getattr__(name: str) -> Any:
    """Resolve the lazily re-exported schema DTOs on first access (module-level ``__getattr__``).

    Raises ``AttributeError`` for any other missing attribute, preserving normal module semantics.
    """
    module_name = _LAZY_DTO_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
