import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.mcp_endpoint import normalize_mcp_url

_PLACEHOLDER_PREFIXES = ("replace-with-", "replace-this-")
_DEFAULT_DB_CREDENTIALS = "//bbd:bbd@"
_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")


def is_local_host(host: str) -> bool:
    """Return True for hosts that never face the public internet: loopback/private/link-local IPs, localhost, mDNS/LAN suffixes and single-label names (container service names).

    Used only to decide whether production-grade transport rules (HTTPS, Secure cookies) are required; it is a
    configuration heuristic, not an access-control check.
    """
    name = host.strip("[]").lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith(_LOCAL_SUFFIXES)
    return ip.is_loopback or ip.is_private or ip.is_link_local


class Settings(BaseSettings):
    """Validated application configuration loaded from environment and the optional .env file; secret fields are masked in representations."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Mode: "development"/"dev"/"test" relax production checks; "production" enforces them; "auto" (default) picks
    # development when PUBLIC_ORIGIN is a local/LAN host and production otherwise. Unknown values fail closed to production.
    environment: str = Field(default="auto", validation_alias="ENVIRONMENT")
    database_url: str = Field(default="postgresql+asyncpg://bbd:bbd@postgres:5432/bbd", repr=False)
    # Direct (non-pooled) PostgreSQL URL for migrations and session-level features (session advisory locks).
    # Required when PGBOUNCER_ENABLED=true, where DATABASE_URL points at PgBouncer in transaction-pooling mode.
    database_direct_url: str | None = Field(default=None, validation_alias="DATABASE_DIRECT_URL", repr=False)
    # Direct (never pooled) URL that logs in as the dedicated read-only role ``umwelt_chat_sql_login`` (migration
    # 0017); the chat SQL tool runs model-written SQL only on it. Unset disables SQL mode (it never falls back to
    # database_url).
    chat_sql_reader_database_url: str | None = Field(default=None, validation_alias="CHAT_SQL_READER_DATABASE_URL", repr=False)
    pgbouncer_enabled: bool = Field(default=False, validation_alias="PGBOUNCER_ENABLED")
    # Pool bound of the small direct engine that owns session advisory leases (only used in PgBouncer mode).
    db_direct_pool_size: int = Field(default=4, ge=1, le=100, validation_alias="DB_DIRECT_POOL_SIZE")
    redis_url: str = Field(default="redis://redis:6379/0", repr=False)
    # Redis Sentinel HA: "host1:26379,host2:26379". Unset keeps the single-instance redis_url. The password,
    # username and database of redis_url still authenticate against the elected master.
    redis_sentinels: str | None = Field(default=None, validation_alias="REDIS_SENTINELS")
    redis_master_name: str = Field(default="mymaster", min_length=1, validation_alias="REDIS_MASTER_NAME")
    redis_sentinel_password: SecretStr = Field(default=SecretStr(""), validation_alias="REDIS_SENTINEL_PASSWORD", repr=False)
    graph_enabled: bool = Field(default=False, validation_alias="GRAPH_ENABLED")
    graph_host: str = Field(default="graph", validation_alias="GRAPH_HOST", repr=False)
    graph_port: int = Field(default=7687, ge=1, le=65535, validation_alias="GRAPH_PORT")
    graph_username: str | None = Field(default="neo4j", validation_alias="GRAPH_USERNAME", repr=False)
    graph_password: SecretStr = Field(default=SecretStr(""), validation_alias="GRAPH_PASSWORD", repr=False)
    graph_database: str = Field(default="neo4j", validation_alias="GRAPH_DATABASE")
    graph_embedding_dimensions: int | None = Field(default=None, ge=1, le=4096, validation_alias="GRAPH_EMBEDDING_DIMENSIONS")
    db_pool_size: int = Field(default=10, ge=1, le=100, validation_alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, ge=0, le=100, validation_alias="DB_MAX_OVERFLOW")
    db_statement_timeout_ms: int = Field(default=60_000, ge=0, validation_alias="DB_STATEMENT_TIMEOUT_MS")
    db_idle_tx_timeout_ms: int = Field(default=240_000, ge=0, validation_alias="DB_IDLE_TX_TIMEOUT_MS")
    # Worker roles run longer set-based maintenance statements than request handlers, but model/provider I/O never
    # happens inside a DB statement, so a bounded timeout is safe and stops a runaway statement holding a slot.
    db_worker_statement_timeout_ms: int = Field(default=120_000, ge=0, validation_alias="DB_WORKER_STATEMENT_TIMEOUT_MS")
    db_model_worker_statement_timeout_ms: int = Field(
        default=120_000, ge=0, validation_alias="DB_MODEL_WORKER_STATEMENT_TIMEOUT_MS",
    )
    max_request_body_bytes: int = Field(default=5 * 1024 * 1024, gt=0, validation_alias="MAX_REQUEST_BODY_BYTES")
    # Most presentation widgets (parts) one assistant turn may carry; extra tool calls are ignored.
    generative_ui_max_parts: int = Field(default=4, ge=1, le=8, validation_alias="GENERATIVE_UI_MAX_PARTS")
    # Most model rounds of one turn that may add widgets (a round after widgets lets the model write text or add more).
    generative_ui_max_rounds: int = Field(default=3, ge=1, le=5, validation_alias="GENERATIVE_UI_MAX_ROUNDS")
    data_dir: Path = Path("/data")
    # Original-file backend (core.storage.factory registry): the local filesystem, or S3-compatible object
    # storage (AWS S3, MinIO) fenced by PostgreSQL. See docs/deployment.md "S3 / MinIO storage".
    storage_backend: Literal["local", "s3"] = Field(default="local", validation_alias="STORAGE_BACKEND")
    s3_endpoint_url: str | None = Field(default=None, validation_alias="S3_ENDPOINT_URL")  # MinIO / non-AWS endpoint
    s3_bucket: str = Field(default="", validation_alias="S3_BUCKET")
    s3_region: str = Field(default="us-east-1", validation_alias="S3_REGION")
    s3_access_key_id: SecretStr = Field(default=SecretStr(""), validation_alias="S3_ACCESS_KEY_ID", repr=False)
    s3_secret_access_key: SecretStr = Field(default=SecretStr(""), validation_alias="S3_SECRET_ACCESS_KEY", repr=False)
    s3_prefix: str = Field(default="", validation_alias="S3_PREFIX")  # optional key prefix inside the bucket
    # "", "AES256" (SSE-S3) or "aws:kms" (SSE-KMS, optionally with S3_SSE_KMS_KEY_ID).
    s3_sse: Literal["", "AES256", "aws:kms"] = Field(default="", validation_alias="S3_SSE")
    s3_sse_kms_key_id: str = Field(default="", validation_alias="S3_SSE_KMS_KEY_ID")
    s3_path_style: bool = Field(default=True, validation_alias="S3_PATH_STYLE")  # MinIO needs path-style addressing
    # If-None-Match: * conditional create; disable for stores without it (publication then uses a staging key + copy).
    s3_conditional_put: bool = Field(default=True, validation_alias="S3_CONDITIONAL_PUT")
    s3_part_size_bytes: int = Field(default=8 * 1024 * 1024, ge=5 * 1024 * 1024, le=512 * 1024 * 1024, validation_alias="S3_PART_SIZE_BYTES")
    # Optional presigned downloads (default off: bytes stream through the API behind authorization + nosniff).
    storage_presigned_downloads: bool = Field(default=False, validation_alias="STORAGE_PRESIGNED_DOWNLOADS")
    storage_presigned_ttl_seconds: int = Field(default=60, ge=1, le=60, validation_alias="STORAGE_PRESIGNED_TTL_SECONDS")
    s3_presign_endpoint_url: str | None = Field(default=None, validation_alias="S3_PRESIGN_ENDPOINT_URL")  # browser-reachable host
    upload_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024)
    parser_timeout_seconds: int = Field(default=120, gt=0, le=3600)
    # How long a chat run parks on `request_user_input`; modules/chat/user_input.py clamps it to the worker job budget.
    chat_input_request_ttl_seconds: int = Field(default=900, ge=30, le=3600)
    # Token flushes always re-lock the cited evidence (lock-only projection) but run the full revalidation
    # (separate exact-selection pass + content-row reads) at most this often; 0 = every flush.
    chat_flush_full_revalidate_seconds: float = Field(
        default=2.0, ge=0.0, le=30.0, validation_alias="CHAT_FLUSH_FULL_REVALIDATE_SECONDS",
    )
    docx_expanded_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pdf_page_max: int = Field(default=500, gt=0)
    # 10 MiB of text chunks in ~1 s and ~0.3 GiB; the 100 MiB docx bound would need ~3 GiB on an 8 GiB host.
    parsed_text_max_chars: int = Field(default=10 * 1024 * 1024, gt=0)
    storage_orphan_grace_seconds: int = Field(default=3600, gt=0)
    # Delivered/failed event_outbox rows of every type are purged after this many days (stage events: 7 days).
    outbox_retention_days: int = Field(default=30, ge=7, le=3650)
    # Gates dispatch_due_collections (gate line applied in C5-api-web); existing constants stay constants.
    collector_scheduler_enabled: bool = Field(default=True, validation_alias="COLLECTOR_SCHEDULER_ENABLED")
    browser_service_url: AnyHttpUrl = AnyHttpUrl("http://browser:8001")
    browser_shared_token: SecretStr = SecretStr("")
    n8n_service_url: AnyHttpUrl = Field(default=AnyHttpUrl("http://n8n:5678"), validation_alias="N8N_SERVICE_URL")
    n8n_port: int = Field(default=5678, ge=1, le=65535, validation_alias="N8N_PORT")
    n8n_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="N8N_API_KEY")
    n8n_webhook_token: SecretStr = Field(default=SecretStr(""), validation_alias="N8N_WEBHOOK_TOKEN")
    n8n_encryption_key: SecretStr = Field(default=SecretStr(""), validation_alias="N8N_ENCRYPTION_KEY", repr=False)
    connector_credential_encryption_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="CONNECTOR_CREDENTIAL_ENCRYPTION_KEY", repr=False
    )
    public_origin: AnyHttpUrl = AnyHttpUrl("http://localhost:3000")
    secure_cookies: bool = False
    setup_token: SecretStr = SecretStr("")
    csrf_signing_secret: SecretStr = SecretStr("")
    # Key the per-IP auth limiter on the rightmost X-Forwarded-For entry. Enable only behind a front proxy that
    # OVERWRITES the header with the real client address; otherwise clients can spoof it and dodge the per-IP limit.
    auth_trust_forwarded_for: bool = Field(default=False, validation_alias="AUTH_TRUST_FORWARDED_FOR")
    session_lifetime_hours: int = Field(default=24, gt=0, le=720)
    # Per-session request budgets (core/rate_limit.py); overrides map name -> (limit, window_seconds).
    request_budget_enabled: bool = Field(default=True, validation_alias="REQUEST_BUDGET_ENABLED")
    request_budget_fail_open: bool = Field(default=False, validation_alias="REQUEST_BUDGET_FAIL_OPEN")
    request_budget_overrides: dict[str, tuple[int, int]] = Field(default_factory=dict, validation_alias="REQUEST_BUDGET_OVERRIDES")
    # --- auth hardening (S2) ---
    # Header carrying the real client IP from a trusted front proxy (e.g. CF-Connecting-IP, X-Real-IP). Unset keeps
    # the peer / AUTH_TRUST_FORWARDED_FOR behaviour. Set only when the proxy overwrites it; clients could spoof it.
    auth_client_ip_header: str = Field(default="", validation_alias="AUTH_CLIENT_IP_HEADER")
    session_idle_hours: int = Field(default=8, gt=0, le=720, validation_alias="SESSION_IDLE_HOURS")
    # --- end auth hardening ---
    # Enable only after W4 scope review and V1 security validation; legacy/operator access stays bootstrap-only.
    multi_workspace_enabled: bool = Field(default=False, validation_alias="MULTI_WORKSPACE_ENABLED")
    # Phone prompt picker (QR). Off by default: the anonymous /pick phone routes are LAN-reachable.
    pick_enabled: bool = Field(default=False, validation_alias="PICK_ENABLED")
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    connector_google_client_id: str = Field(default="", validation_alias="CONNECTOR_GOOGLE_CLIENT_ID")
    connector_google_client_secret: SecretStr = Field(default=SecretStr(""), validation_alias="CONNECTOR_GOOGLE_CLIENT_SECRET", repr=False)
    connector_google_callback_url: str = Field(default="", validation_alias="CONNECTOR_GOOGLE_CALLBACK_URL")
    connector_reddit_client_id: str = Field(default="", validation_alias="CONNECTOR_REDDIT_CLIENT_ID")
    connector_reddit_client_secret: SecretStr = Field(default=SecretStr(""), validation_alias="CONNECTOR_REDDIT_CLIENT_SECRET", repr=False)
    connector_reddit_callback_url: str = Field(default="", validation_alias="CONNECTOR_REDDIT_CALLBACK_URL")
    github_app_client_id: str = ""
    github_app_client_secret: SecretStr = SecretStr("")
    github_app_callback_url: str = ""
    github_app_id: str = Field(default="", max_length=19, validation_alias="GITHUB_APP_ID")
    github_app_webhook_secret: SecretStr = Field(default=SecretStr(""), validation_alias="GITHUB_APP_WEBHOOK_SECRET", repr=False)
    github_webhook_receiver_revision: str = Field(default="1", min_length=1, max_length=64, validation_alias="GITHUB_WEBHOOK_RECEIVER_REVISION")
    model_gateway_slots: int = Field(default=24, ge=1, le=1000, validation_alias="MODEL_GATEWAY_SLOTS")
    # Per-call ModelGateway timeout for background entity/timeline extraction.
    background_llm_timeout_seconds: int = Field(default=60, ge=5, le=600, validation_alias="BACKGROUND_LLM_TIMEOUT_SECONDS")
    # arq job_timeout of the dedicated model worker (extraction + translation).
    model_worker_job_timeout_seconds: int = Field(default=900, ge=60, le=3600, validation_alias="MODEL_WORKER_JOB_TIMEOUT_SECONDS")
    # Chunks per embedding request in search indexing (1 = legacy one-chunk requests); clamped to 128.
    search_embed_batch_size: int = Field(default=64, ge=1, le=128, validation_alias="SEARCH_EMBED_BATCH_SIZE")
    ai_credential_encryption_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="AI_CREDENTIAL_ENCRYPTION_KEY", repr=False
    )
    # Key rotation: decrypt-only fallback key; new ciphertext always uses the primary key above.
    ai_credential_encryption_key_secondary: SecretStr = Field(
        default=SecretStr(""), validation_alias="AI_CREDENTIAL_ENCRYPTION_KEY_SECONDARY", repr=False
    )
    ai_allowed_endpoint_hosts: set[str] = Field(
        default_factory=set, validation_alias="AI_ALLOWED_ENDPOINT_HOSTS", repr=False
    )
    ai_allowed_endpoint_cidrs: list[str] = Field(
        default_factory=list, validation_alias="AI_ALLOWED_ENDPOINT_CIDRS", repr=False
    )
    # Operator opt-in: cloud providers (https, public hostnames) bypass the host/CIDR allowlists; private ranges stay allowlist-only.
    ai_allow_public_providers: bool = Field(default=False, validation_alias="AI_ALLOW_PUBLIC_PROVIDERS")
    # This administrator-only map is separate from AI gateway egress exceptions.
    mcp_allowed_endpoint_cidrs: dict[tuple[str, str, int], tuple[str, ...]] = Field(
        default_factory=dict, validation_alias="MCP_ALLOWED_ENDPOINT_CIDRS", repr=False
    )
    # Observability (docs/observability.md). /metrics: bearer METRICS_TOKEN when set; with no token it is served only
    # to private/loopback direct clients outside production and is otherwise disabled (404).
    metrics_token: SecretStr = Field(default=SecretStr(""), validation_alias="METRICS_TOKEN", repr=False)
    # Worker /metrics HTTP port (0 = off). The chat worker uses port+1 and the model worker port+2.
    metrics_worker_port: int = Field(default=0, ge=0, le=65533, validation_alias="METRICS_WORKER_PORT")
    metrics_worker_bind: str = Field(default="127.0.0.1", validation_alias="METRICS_WORKER_BIND")
    # OTLP/HTTP traces endpoint (e.g. http://otel-collector:4318); empty = tracing fully off (nothing imported or patched).
    otel_exporter_otlp_endpoint: str = Field(default="", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_traces_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0, validation_alias="OTEL_TRACES_SAMPLE_RATIO")
    # Widget MCP server (modules/mcp_widgets): off by default; when enabled a bearer token is mandatory.
    mcp_widgets_enabled: bool = Field(default=False, validation_alias="MCP_WIDGETS_ENABLED")
    mcp_widgets_token: SecretStr = Field(default=SecretStr(""), validation_alias="MCP_WIDGETS_TOKEN", repr=False)
    # Exact origin of the host page embedding assembled documents (bridge postMessage target); empty uses public_origin.
    mcp_widgets_parent_origin: str = Field(default="", validation_alias="MCP_WIDGETS_PARENT_ORIGIN")
    # Comma-separated exact browser origins allowed to call /mcp cross-origin (CORS); empty = no CORS. Never "*".
    mcp_widgets_allowed_origins: str = Field(default="", validation_alias="MCP_WIDGETS_ALLOWED_ORIGINS")
    mcp_stdio_profile_manifest: str = Field(
        default="", validation_alias="MCP_STDIO_PROFILE_MANIFEST", repr=False
    )
    webhook_profiles_json: str = Field(default="", validation_alias="WEBHOOK_PROFILES", repr=False)
    # Extra CIDRs for web search egress only (test compose); never the gateway CIDRs (review P3-8).
    web_search_allowed_cidrs: list[str] = Field(
        default_factory=list, validation_alias="WEB_SEARCH_ALLOWED_CIDRS", repr=False
    )
    # Optional per-alias HMAC signing secrets for inbound automation webhooks (JSON object alias -> secret).
    automation_webhook_signing_secrets: dict[str, SecretStr] = Field(
        default_factory=dict, validation_alias="AUTOMATION_WEBHOOK_SIGNING_SECRETS", repr=False
    )
    web_search_daily_limit: int = Field(default=50, ge=0, le=1000, validation_alias="WEB_SEARCH_DAILY_LIMIT")
    approval_expiry_hours: int = Field(default=24, ge=1, le=72, validation_alias="APPROVAL_EXPIRY_HOURS")

    @field_validator("ai_allowed_endpoint_hosts")
    @classmethod
    def normalize_endpoint_hosts(cls, values: set[str]) -> set[str]:
        """Normalize approved gateway host or host:port entries and reject credentials, paths, malformed ports, scoped IPv6, and invalid authorities."""
        normalized = set()
        for value in values:
            parts = urlsplit(f"//{value}")
            if not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment:
                raise ValueError("AI_ALLOWED_ENDPOINT_HOSTS entries must be hostnames or host:port pairs")
            try:
                address = ipaddress.ip_address(parts.hostname)
            except ValueError:
                host = parts.hostname.encode("idna").decode("ascii").lower()
                authority_host = host
            else:
                if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
                    raise ValueError("Scoped IPv6 endpoint hosts are not supported")
                host = address.compressed.lower()
                authority_host = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
            try:
                port = parts.port
            except ValueError as exc:
                raise ValueError("AI_ALLOWED_ENDPOINT_HOSTS contains an invalid port") from exc
            if port == 0:
                raise ValueError("AI_ALLOWED_ENDPOINT_HOSTS ports must be between 1 and 65535")
            normalized.add(f"{authority_host}:{port}" if port is not None else authority_host)
        return normalized

    @field_validator("ai_allowed_endpoint_cidrs", "web_search_allowed_cidrs")
    @classmethod
    def normalize_endpoint_cidrs(cls, values: list[str]) -> list[str]:
        """Parse, canonicalize, deduplicate, and sort approved endpoint CIDRs; reject IPv4-mapped IPv6 networks."""
        networks = []
        for value in values:
            network = ipaddress.ip_network(value, strict=False)
            if isinstance(network, ipaddress.IPv6Network) and network.network_address.ipv4_mapped is not None:
                raise ValueError("IPv4-mapped IPv6 CIDRs must use the equivalent IPv4 CIDR")
            networks.append(network.with_prefixlen)
        return sorted(set(networks))

    @field_validator("web_search_allowed_cidrs")
    @classmethod
    def reject_broad_web_search_cidrs(cls, values: list[str]) -> list[str]:
        """Reject ranges so broad (IPv4 shorter than /8, IPv6 shorter than /16) that they would disable the public-address guard."""
        for value in values:
            network = ipaddress.ip_network(value, strict=False)
            if network.prefixlen < (8 if network.version == 4 else 16):
                raise ValueError("WEB_SEARCH_ALLOWED_CIDRS ranges must be at least /8 (IPv4) or /16 (IPv6)")
        return values

    @field_validator("mcp_allowed_endpoint_cidrs", mode="before")
    @classmethod
    def normalize_mcp_endpoint_origins(cls, values: object) -> object:
        """Convert deployment origin keys to the exact normalized tuple consumed by MCP transport."""
        if not isinstance(values, dict):
            raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS must be a JSON object")
        if len(values) > 64:
            raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS supports at most 64 exact origins")
        normalized: dict[tuple[str, str, int], object] = {}
        for raw_origin, cidrs in values.items():
            if not isinstance(raw_origin, str):
                raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS keys must be origin strings")
            scheme, host, port, _path = normalize_mcp_url(raw_origin, origin_only=True)
            origin = (scheme, host, port)
            if origin in normalized:
                raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS contains duplicate normalized origins")
            normalized[origin] = cidrs
        return normalized

    @field_validator("mcp_allowed_endpoint_cidrs")
    @classmethod
    def validate_mcp_endpoint_cidrs(
        cls, values: dict[tuple[str, str, int], tuple[str, ...]]
    ) -> dict[tuple[str, str, int], tuple[str, ...]]:
        """Canonicalize bounded private or loopback destination networks for exact MCP origins."""
        private_networks = tuple(
            ipaddress.ip_network(value)
            for value in (
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "fc00::/7", "127.0.0.0/8", "::1/128",
            )
        )
        normalized: dict[tuple[str, str, int], tuple[str, ...]] = {}
        for origin, values_for_origin in values.items():
            if not values_for_origin:
                raise ValueError("Each MCP endpoint exception must approve at least one CIDR")
            if len(values_for_origin) > 16:
                raise ValueError("Each MCP endpoint may approve at most 16 CIDRs")
            networks = []
            for value in values_for_origin:
                network = ipaddress.ip_network(value, strict=False)
                if (network.prefixlen == 0 or
                        isinstance(network, ipaddress.IPv6Network)
                        and network.network_address.ipv4_mapped is not None or
                        not any(
                            (isinstance(network, ipaddress.IPv4Network) and isinstance(approved, ipaddress.IPv4Network)
                             and network.subnet_of(approved))
                            or (isinstance(network, ipaddress.IPv6Network) and isinstance(approved, ipaddress.IPv6Network)
                                and network.subnet_of(approved))
                            for approved in private_networks
                        )):
                    raise ValueError("MCP endpoint CIDRs must be bounded private or loopback networks")
                networks.append(network.with_prefixlen)
            normalized[origin] = tuple(sorted(set(networks)))
        return normalized

    @field_validator("public_origin")
    @classmethod
    def require_origin_only(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require the public origin to contain only scheme and host, without credentials, path, query, or fragment."""
        if value.path not in ("", "/") or value.query or value.fragment or value.username:
            raise ValueError("public_origin must contain only scheme and host")
        return value

    @field_validator("mcp_widgets_parent_origin")
    @classmethod
    def validate_widgets_parent_origin(cls, value: str) -> str:
        """Accept empty or one exact http(s) origin; wildcards, paths and credentials would weaken the bridge target."""
        value = value.strip().rstrip("/")
        if value:
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https") or not parts.hostname or "*" in value or parts.path or parts.query or parts.fragment or parts.username:
                raise ValueError("MCP_WIDGETS_PARENT_ORIGIN must be one exact http(s) origin")
        return value

    @field_validator("mcp_widgets_allowed_origins")
    @classmethod
    def validate_widgets_allowed_origins(cls, value: str) -> str:
        """Accept empty or comma-separated exact http(s) origins; wildcards would expose a bearer-protected endpoint."""
        for item in (v.strip().rstrip("/") for v in value.split(",") if v.strip()):
            parts = urlsplit(item)
            if parts.scheme not in ("http", "https") or not parts.hostname or "*" in item or parts.path or parts.query or parts.fragment or parts.username:
                raise ValueError("MCP_WIDGETS_ALLOWED_ORIGINS must list exact http(s) origins")
        return value

    @field_validator("github_app_id")
    @classmethod
    def validate_github_app_id(cls, value: str) -> str:
        """Accept only an optional positive signed-64-bit decimal GitHub App ID."""
        if value and (not value.isdecimal() or value.startswith("0") or int(value) > 2**63 - 1):
            raise ValueError("GITHUB_APP_ID must be a positive decimal ID")
        return value

    @property
    def origin_is_local(self) -> bool:
        """Return True when PUBLIC_ORIGIN points at a loopback, LAN or single-label host (see is_local_host)."""
        return is_local_host(self.public_origin.host or "")

    @property
    def mode(self) -> str:
        """Resolve ENVIRONMENT to "development", "test" or "production".

        "auto" (default) is development for a local/LAN PUBLIC_ORIGIN and production for any public domain; an
        explicit value always wins, and unrecognised values fail closed to production.
        """
        value = self.environment.strip().lower()
        if value in {"dev", "development"}:
            return "development"
        if value == "test":
            return "test"
        if value == "auto":
            return "development" if self.origin_is_local else "production"
        return "production"

    @property
    def has_trusted_client_ip_source(self) -> bool:
        """Return True when a front proxy is declared as the source of the real client address.

        Either AUTH_CLIENT_IP_HEADER names a non-blank header or AUTH_TRUST_FORWARDED_FOR is on.
        """
        return bool(self.auth_client_ip_header.strip()) or self.auth_trust_forwarded_for

    def require_trusted_client_ip_source(self) -> None:
        """Refuse API startup in production with a public origin and no trusted client-IP source.

        uvicorn runs with --no-proxy-headers, so without AUTH_CLIENT_IP_HEADER / AUTH_TRUST_FORWARDED_FOR every
        request appears to come from the web container and the per-IP login limit plus account lockout become
        instance-global (one attacker could lock the owner out). Only the API keys on client IP, so this is called
        from create_app and deliberately is NOT part of the Settings validator: workers, chat/model workers and
        migrations build Settings too and never see client addresses.
        Raises RuntimeError naming the missing variables. Secrets-relaxed (dev/test) and local origins are exempt.
        """
        if self.secrets_relaxed or self.is_dev or self.origin_is_local or self.has_trusted_client_ip_source:
            return
        raise RuntimeError(
            "AUTH_CLIENT_IP_HEADER or AUTH_TRUST_FORWARDED_FOR=true must be configured for a public "
            "production origin (see docs/deployment.md, Client IP)"
        )

    @property
    def is_dev(self) -> bool:
        """Return True in development or test mode (relaxed secrets/cookie rules, API docs enabled)."""
        return self.mode != "production"

    @property
    def secrets_relaxed(self) -> bool:
        """Return True only when ENVIRONMENT is explicitly dev/development/test.

        "auto" never relaxes the placeholder-secret and default-database checks: a mis-set local-looking PUBLIC_ORIGIN
        on a real server must not silently accept template credentials. "auto" relaxes transport rules only.
        """
        return self.environment.strip().lower() in {"dev", "development", "test"}

    @property
    def admin_database_url(self) -> str:
        """Return the URL for one-shot admin tools and migrations: the direct URL when set, else ``database_url``.

        Admin processes are short-lived and may use session features, so they never go through a transaction pooler.
        """
        return self.database_direct_url or self.database_url

    @model_validator(mode="after")
    def require_direct_url_with_pgbouncer(self) -> "Settings":
        """Require ``DATABASE_DIRECT_URL`` whenever PgBouncer mode is on.

        Session advisory leases and migrations cannot run through a transaction pooler; falling back to the
        pooled URL would silently break their mutual exclusion, so startup fails instead.
        """
        if self.pgbouncer_enabled and not self.database_direct_url:
            raise ValueError("DATABASE_DIRECT_URL is required when PGBOUNCER_ENABLED is true")
        return self

    @model_validator(mode="after")
    def require_s3_bucket_and_credentials(self) -> "Settings":
        """Fail startup when ``STORAGE_BACKEND=s3`` lacks a bucket or only one half of the static key pair is set.

        Both credentials empty is allowed: the AWS default credential chain (instance/role) then applies.
        """
        if self.storage_backend != "s3":
            return self
        if not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        if bool(self.s3_access_key_id.get_secret_value()) != bool(self.s3_secret_access_key.get_secret_value()):
            raise ValueError("S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together")
        return self

    @model_validator(mode="after")
    def reject_insecure_production_config(self) -> "Settings":
        """Refuse startup in production mode when a public origin is not HTTPS, cookies are insecure on an https origin, secrets are template placeholders, or the default database credentials remain.

        Local/LAN origins (see is_local_host) may use plain http with non-Secure cookies, including under "auto".
        Placeholder/default credentials are rejected unless the environment is explicitly dev/test (secrets_relaxed).
        A redis_url without a password is rejected under the same condition.
        Raises ValueError (surfaced as ValidationError) naming the offending field, never its value.
        """
        if self.secrets_relaxed:
            return self
        if not self.is_dev:  # transport rules: skipped for a local origin under "auto"
            if self.public_origin.scheme != "https" and not self.origin_is_local:
                raise ValueError("PUBLIC_ORIGIN must be https for a non-local host")
            if self.public_origin.scheme == "https" and not self.secure_cookies:
                raise ValueError("SECURE_COOKIES must be true when PUBLIC_ORIGIN is https")
        # Redis holds sessions, budgets and queues: an unauthenticated instance is refused outside explicit dev/test.
        # Same rule as placeholder secrets, so "auto" does not relax it (setup tooling generates REDISCLI_AUTH).
        if not urlsplit(self.redis_url).password:
            raise ValueError("redis_url must include a password (redis://:<REDISCLI_AUTH>@host:port/db)")
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                text = value.get_secret_value()
                bad = text.startswith(_PLACEHOLDER_PREFIXES)
            elif name in {"database_url", "redis_url", "chat_sql_reader_database_url"} and value:
                text = str(value)
                bad = any(prefix in text for prefix in _PLACEHOLDER_PREFIXES) or (
                    name == "database_url" and _DEFAULT_DB_CREDENTIALS in text
                )
            else:
                continue
            if bad:
                raise ValueError(f"{name} uses a placeholder or default credential; set a real value")
        return self
