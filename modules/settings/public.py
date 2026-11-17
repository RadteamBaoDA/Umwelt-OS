"""Settings owner public contract: AI/provider, web-search, n8n, viz-router, preferences and translation settings.

Other modules import this module only. The implementation lives in the private ``_settings_*`` submodules (``_settings_common`` holds shared endpoint/cipher/scope/provider helpers); every name that was importable from the former single module stays importable here."""

from __future__ import annotations

from modules.settings._settings.ai_save import (
    ROLE_MODEL_TYPE_LABEL as ROLE_MODEL_TYPE_LABEL,
)
from modules.settings._settings.ai_save import (
    _build_provider_records as _build_provider_records,
)
from modules.settings._settings.ai_save import (
    _chosen_defaults as _chosen_defaults,
)
from modules.settings._settings.ai_save import (
    _consented_privacy as _consented_privacy,
)
from modules.settings._settings.ai_save import (
    _kept_ciphertext as _kept_ciphertext,
)
from modules.settings._settings.ai_save import (
    _provider_endpoint as _provider_endpoint,
)
from modules.settings._settings.ai_save import (
    _validated_role_mapping as _validated_role_mapping,
)
from modules.settings._settings.ai_save import (
    role_accepts as role_accepts,
)
from modules.settings._settings.ai_save import (
    save_ai_settings as save_ai_settings,
)
from modules.settings._settings.common import (
    VIZ_CLOUDFLARE_ACCOUNT as VIZ_CLOUDFLARE_ACCOUNT,
)
from modules.settings._settings.common import (
    VIZ_CLOUDFLARE_URL as VIZ_CLOUDFLARE_URL,
)
from modules.settings._settings.common import (
    VIZ_KEYED_KINDS as VIZ_KEYED_KINDS,
)
from modules.settings._settings.common import (
    VIZ_OPENROUTER_URL as VIZ_OPENROUTER_URL,
)
from modules.settings._settings.common import (
    VIZ_ROUTER_ROLE as VIZ_ROUTER_ROLE,
)
from modules.settings._settings.common import (
    VIZ_TYPESAFE_DESTINATION as VIZ_TYPESAFE_DESTINATION,
)
from modules.settings._settings.common import (
    _cipher as _cipher,
)
from modules.settings._settings.common import (
    _decrypt as _decrypt,
)
from modules.settings._settings.common import (
    _destination as _destination,
)
from modules.settings._settings.common import (
    _endpoint as _endpoint,
)
from modules.settings._settings.common import (
    _fingerprint as _fingerprint,
)
from modules.settings._settings.common import (
    _identity as _identity,
)
from modules.settings._settings.common import (
    _models as _models,
)
from modules.settings._settings.common import (
    _privacy as _privacy,
)
from modules.settings._settings.common import (
    _resolve_provider as _resolve_provider,
)
from modules.settings._settings.common import (
    _row as _row,
)
from modules.settings._settings.common import (
    _stored_providers as _stored_providers,
)
from modules.settings._settings.common import (
    _web_destination as _web_destination,
)
from modules.settings._settings.common import (
    credential_cipher as credential_cipher,
)
from modules.settings._settings.common import (
    derive_for_mapping as derive_for_mapping,
)
from modules.settings._settings.common import (
    derive_for_role as derive_for_role,
)
from modules.settings._settings.common import (
    mapping_for_override as mapping_for_override,
)
from modules.settings._settings.common import (
    require_preferences_account as require_preferences_account,
)
from modules.settings._settings.common import (
    require_settings_scope as require_settings_scope,
)
from modules.settings._settings.common import (
    resolve_draft_endpoint as resolve_draft_endpoint,
)
from modules.settings._settings.common import (
    resolve_draft_provider as resolve_draft_provider,
)
from modules.settings._settings.common import (
    scope_actor as scope_actor,
)
from modules.settings._settings.common import (
    validate_endpoint as validate_endpoint,
)
from modules.settings._settings.execution import (
    _prompts_read as _prompts_read,
)
from modules.settings._settings.execution import (
    _read as _read,
)
from modules.settings._settings.execution import (
    check_ai_execution_config as check_ai_execution_config,
)
from modules.settings._settings.execution import (
    get_ai_execution_config as get_ai_execution_config,
)
from modules.settings._settings.execution import (
    lock_ai_settings_for_share as lock_ai_settings_for_share,
)
from modules.settings._settings.execution import (
    read_ai_settings as read_ai_settings,
)
from modules.settings._settings.execution import (
    read_system_prompts as read_system_prompts,
)
from modules.settings._settings.execution import (
    save_system_prompts as save_system_prompts,
)
from modules.settings._settings.n8n import (
    N8nResolved as N8nResolved,
)
from modules.settings._settings.n8n import (
    _n8n_revision as _n8n_revision,
)
from modules.settings._settings.n8n import (
    _n8n_stored as _n8n_stored,
)
from modules.settings._settings.n8n import (
    n8n_enabled as n8n_enabled,
)
from modules.settings._settings.n8n import (
    n8n_http_client as n8n_http_client,
)
from modules.settings._settings.n8n import (
    n8n_service_url as n8n_service_url,
)
from modules.settings._settings.n8n import (
    read_n8n as read_n8n,
)
from modules.settings._settings.n8n import (
    resolve_n8n as resolve_n8n,
)
from modules.settings._settings.n8n import (
    save_n8n as save_n8n,
)
from modules.settings._settings.preferences import (
    _resolved_display_name as _resolved_display_name,
)
from modules.settings._settings.preferences import (
    _translation_settings_read as _translation_settings_read,
)
from modules.settings._settings.preferences import (
    get_translation_settings as get_translation_settings,
)
from modules.settings._settings.preferences import (
    module_dependency as module_dependency,
)
from modules.settings._settings.preferences import (
    module_is_enabled as module_is_enabled,
)
from modules.settings._settings.preferences import (
    read_ai_policy_fingerprint as read_ai_policy_fingerprint,
)
from modules.settings._settings.preferences import (
    read_module_availability as read_module_availability,
)
from modules.settings._settings.preferences import (
    read_owner_preferences as read_owner_preferences,
)
from modules.settings._settings.preferences import (
    read_retention_settings as read_retention_settings,
)
from modules.settings._settings.preferences import (
    read_translation_settings_projection as read_translation_settings_projection,
)
from modules.settings._settings.preferences import (
    save_owner_preferences as save_owner_preferences,
)
from modules.settings._settings.preferences import (
    save_translation_settings_cas as save_translation_settings_cas,
)
from modules.settings._settings.preferences import (
    set_initial_owner_display_name as set_initial_owner_display_name,
)
from modules.settings._settings.viz import (
    VizRouterResolved as VizRouterResolved,
)
from modules.settings._settings.viz import (
    _apply_viz_kind_fields as _apply_viz_kind_fields,
)
from modules.settings._settings.viz import (
    _viz_destination as _viz_destination,
)
from modules.settings._settings.viz import (
    _viz_endpoint as _viz_endpoint,
)
from modules.settings._settings.viz import (
    _viz_key_ciphertexts as _viz_key_ciphertexts,
)
from modules.settings._settings.viz import (
    _viz_stored as _viz_stored,
)
from modules.settings._settings.viz import (
    read_viz_router as read_viz_router,
)
from modules.settings._settings.viz import (
    resolve_viz_router as resolve_viz_router,
)
from modules.settings._settings.viz import (
    save_viz_router as save_viz_router,
)
from modules.settings._settings.viz import (
    saved_openrouter_key as saved_openrouter_key,
)
from modules.settings._settings.viz import (
    viz_decision_discovery_config as viz_decision_discovery_config,
)
from modules.settings._settings.viz import (
    viz_router_gateway_config as viz_router_gateway_config,
)
from modules.settings._settings.web_search import (
    read_web_search as read_web_search,
)
from modules.settings._settings.web_search import (
    save_web_search as save_web_search,
)
from modules.settings.display_name import normalize_display_name
from modules.settings.models import capability_evidence as capability_evidence
from modules.settings.models import list_capabilities as list_capabilities
from modules.settings.models import list_model_test_results as list_model_test_results
from modules.settings.models import new_capability_result as new_capability_result
from modules.settings.models import save_capability as save_capability
from modules.settings.models import save_model_test_result as save_model_test_result
from modules.settings.prompts import DEFAULT_SYSTEM_PROMPTS, resolve_prompt
from modules.settings.schemas import MaintenanceSummaryRead as MaintenanceSummaryRead
from modules.settings.schemas import TranslationSettingsRead as TranslationSettingsRead
from modules.settings.schemas import TranslationSettingsUpdate as TranslationSettingsUpdate

# Names re-exported from the owner's private submodules for other modules (see top of file).
__all__ = [
    "DEFAULT_SYSTEM_PROMPTS",
    "normalize_display_name",
    "resolve_prompt",
]
