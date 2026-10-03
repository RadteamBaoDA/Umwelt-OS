from pathlib import Path
import ipaddress
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application configuration loaded from environment and the optional .env file; secret fields are masked in representations."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(default="postgresql+asyncpg://bbd:bbd@postgres:5432/bbd", repr=False)
    redis_url: str = Field(default="redis://redis:6379/0", repr=False)
    graph_enabled: bool = Field(default=False, validation_alias="GRAPH_ENABLED")
    graph_host: str = Field(default="graph", validation_alias="GRAPH_HOST", repr=False)
    graph_port: int = Field(default=6379, ge=1, le=65535, validation_alias="GRAPH_PORT")
    graph_username: str | None = Field(default=None, validation_alias="GRAPH_USERNAME", repr=False)
    graph_password: SecretStr = Field(default=SecretStr(""), validation_alias="GRAPH_PASSWORD", repr=False)
    graph_database: str = Field(default="bbd_temporal", validation_alias="GRAPH_DATABASE")
    graph_embedding_dimensions: int | None = Field(default=None, ge=1, le=4096, validation_alias="GRAPH_EMBEDDING_DIMENSIONS")
    data_dir: Path = Path("/data")
    upload_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024)
    parser_timeout_seconds: int = Field(default=120, gt=0, le=3600)
    docx_expanded_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pdf_page_max: int = Field(default=500, gt=0)
    storage_orphan_grace_seconds: int = Field(default=3600, gt=0)
    browser_service_url: AnyHttpUrl = AnyHttpUrl("http://browser:8001")
    browser_shared_token: SecretStr = SecretStr("")
    n8n_service_url: AnyHttpUrl = Field(default=AnyHttpUrl("http://n8n:5678"), validation_alias="N8N_SERVICE_URL")
    n8n_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="N8N_API_KEY")
    n8n_webhook_token: SecretStr = Field(default=SecretStr(""), validation_alias="N8N_WEBHOOK_TOKEN")
    connector_credential_encryption_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="CONNECTOR_CREDENTIAL_ENCRYPTION_KEY", repr=False
    )
    public_origin: AnyHttpUrl = AnyHttpUrl("http://localhost:3000")
    secure_cookies: bool = False
    setup_token: SecretStr = SecretStr("")
    csrf_signing_secret: SecretStr = SecretStr("")
    session_lifetime_hours: int = Field(default=24, gt=0, le=720)
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    omniroute_base_url: AnyHttpUrl | None = Field(default=None, repr=False)
    omniroute_api_key: SecretStr = SecretStr("")
    omniroute_models: dict[str, str] = Field(default_factory=dict, repr=False)
    ai_credential_encryption_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="AI_CREDENTIAL_ENCRYPTION_KEY", repr=False
    )
    ai_allowed_endpoint_hosts: set[str] = Field(
        default_factory=set, validation_alias="AI_ALLOWED_ENDPOINT_HOSTS", repr=False
    )
    ai_allowed_endpoint_cidrs: list[str] = Field(
        default_factory=list, validation_alias="AI_ALLOWED_ENDPOINT_CIDRS", repr=False
    )

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

    @field_validator("ai_allowed_endpoint_cidrs")
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

    @field_validator("omniroute_base_url", mode="before")
    @classmethod
    def blank_gateway_url_is_unconfigured(cls, value: object) -> object:
        """Treat an empty OmniRoute URL as absent before Pydantic URL validation."""
        return None if value == "" else value

    @field_validator("public_origin")
    @classmethod
    def require_origin_only(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require the public origin to contain only scheme and host, without credentials, path, query, or fragment."""
        if value.path not in ("", "/") or value.query or value.fragment or value.username:
            raise ValueError("public_origin must contain only scheme and host")
        return value
