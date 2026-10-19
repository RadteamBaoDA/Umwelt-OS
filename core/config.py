import ipaddress
from pathlib import Path
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
    redis_url: str = Field(default="redis://redis:6379/0", repr=False)
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
    max_request_body_bytes: int = Field(default=5 * 1024 * 1024, gt=0, validation_alias="MAX_REQUEST_BODY_BYTES")
    data_dir: Path = Path("/data")
    upload_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024)
    parser_timeout_seconds: int = Field(default=120, gt=0, le=3600)
    docx_expanded_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pdf_page_max: int = Field(default=500, gt=0)
    # 10 MiB of text chunks in ~1 s and ~0.3 GiB; the 100 MiB docx bound would need ~3 GiB on an 8 GiB host.
    parsed_text_max_chars: int = Field(default=10 * 1024 * 1024, gt=0)
    storage_orphan_grace_seconds: int = Field(default=3600, gt=0)
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
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    github_app_client_id: str = ""
    github_app_client_secret: SecretStr = SecretStr("")
    github_app_callback_url: str = ""
    github_app_id: str = Field(default="", max_length=19, validation_alias="GITHUB_APP_ID")
    github_app_webhook_secret: SecretStr = Field(default=SecretStr(""), validation_alias="GITHUB_APP_WEBHOOK_SECRET", repr=False)
    github_webhook_receiver_revision: str = Field(default="1", min_length=1, max_length=64, validation_alias="GITHUB_WEBHOOK_RECEIVER_REVISION")
    model_gateway_slots: int = Field(default=24, ge=1, le=1000, validation_alias="MODEL_GATEWAY_SLOTS")
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
    # Widget MCP server (modules/mcp_widgets): off by default; when enabled a bearer token is mandatory.
    mcp_widgets_enabled: bool = Field(default=False, validation_alias="MCP_WIDGETS_ENABLED")
    mcp_widgets_token: SecretStr = Field(default=SecretStr(""), validation_alias="MCP_WIDGETS_TOKEN", repr=False)
    # Exact origin of the host page embedding assembled documents (bridge postMessage target); empty uses public_origin.
    mcp_widgets_parent_origin: str = Field(default="", validation_alias="MCP_WIDGETS_PARENT_ORIGIN")
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
            raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS must be a JSON object")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
        if len(values) > 64:
            raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS supports at most 64 exact origins")
        normalized: dict[tuple[str, str, int], object] = {}
        for raw_origin, cidrs in values.items():
            if not isinstance(raw_origin, str):
                raise ValueError("MCP_ALLOWED_ENDPOINT_CIDRS keys must be origin strings")  # noqa: TRY004  # ValueError is part of the contract; TypeError would change behavior
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
            elif name in {"database_url", "redis_url"}:
                text = str(value)
                bad = any(prefix in text for prefix in _PLACEHOLDER_PREFIXES) or (
                    name == "database_url" and _DEFAULT_DB_CREDENTIALS in text
                )
            else:
                continue
            if bad:
                raise ValueError(f"{name} uses a placeholder or default credential; set a real value")
        return self
