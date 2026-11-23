"""Server settings, parsed purely from an injected environment mapping.

``load_settings`` never reads ``os.environ`` itself: the CLI passes it in, and
tests pass a plain dict. Every problem raises ``ConfigurationError`` with a
message that says what to set. Secrets are held as ``pydantic.SecretStr`` so a
``repr`` of the settings (in a log line or a traceback) never shows them.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import SecretStr

from mcp_google_tasks.errors import ConfigurationError
from mcp_google_tasks.logs import LogLevel

type Transport = Literal["http", "stdio"]
type AuthMode = Literal["oauth-proxy", "resource-server", "token"]
type StorageBackend = Literal["default", "filetree", "redis"]

SCOPE_TASKS_FULL = "https://www.googleapis.com/auth/tasks"
SCOPE_TASKS_READONLY = "https://www.googleapis.com/auth/tasks.readonly"
SCOPE_OPENID = "openid"
SCOPE_USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_REDIRECT_PATH = "/auth/callback"
DEFAULT_STORAGE_DIR = "/data/oauth-tokens"
DEFAULT_TOKEN_CACHE_TTL_SECONDS = 300

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_TRANSPORTS: Mapping[str, Transport] = {
    "http": "http",
    "streamable-http": "http",
    "stdio": "stdio",
}
_AUTH_MODES: Mapping[str, AuthMode] = {
    "oauth-proxy": "oauth-proxy",
    "resource-server": "resource-server",
    "token": "token",
}
_STORAGE_BACKENDS: Mapping[str, StorageBackend] = {
    "default": "default",
    "filetree": "filetree",
    "redis": "redis",
}
_LOG_LEVELS: Mapping[str, LogLevel] = {
    "debug": "debug",
    "info": "info",
    "warning": "warning",
    "error": "error",
}


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime configuration.

    Attributes:
        transport: ``http`` (streamable HTTP) or ``stdio``.
        auth_mode: ``oauth-proxy``, ``resource-server`` or ``token``.
        host: HTTP bind address.
        port: HTTP bind port.
        base_url: Public URL of this server (no trailing slash).
        readonly: Request the read-only Tasks scope and hide every write tool.
        log_level: Lowest level written to stderr.
        client_id: Google OAuth client ID.
        client_secret: Google OAuth client secret.
        redirect_path: OAuth callback path for ``oauth-proxy`` mode.
        allowed_client_redirect_uris: MCP client redirect URIs allowed to register
            (``oauth-proxy``); ``None`` allows any.
        audiences: OAuth client IDs whose tokens are accepted (``resource-server``).
        token_cache_ttl_seconds: Seconds a verified token stays cached
            (``resource-server``); 0 disables the cache.
        jwt_signing_key: Key that signs FastMCP-issued JWTs (``oauth-proxy``).
        storage_backend: Where ``oauth-proxy`` keeps upstream tokens.
        storage_encryption_key: Fernet key for tokens at rest.
        storage_dir: Directory for the ``filetree`` backend.
        redis_url: Connection URL for the ``redis`` backend (may embed a password).
        storage_key_prefix: Collection prefix for shared storage.
        access_token: Google access token (``token`` mode).
        refresh_token: Google refresh token (``token`` mode).
        mcp_auth_token: Shared bearer secret required from clients (``token`` mode over HTTP).
    """

    transport: Transport
    auth_mode: AuthMode
    host: str
    port: int
    base_url: str
    readonly: bool
    log_level: LogLevel
    client_id: str | None
    client_secret: SecretStr | None
    redirect_path: str
    allowed_client_redirect_uris: tuple[str, ...] | None
    audiences: tuple[str, ...]
    token_cache_ttl_seconds: int
    jwt_signing_key: SecretStr | None
    storage_backend: StorageBackend
    storage_encryption_key: SecretStr | None
    storage_dir: Path
    redis_url: SecretStr | None
    storage_key_prefix: str | None
    access_token: SecretStr | None
    refresh_token: SecretStr | None
    mcp_auth_token: SecretStr | None

    @property
    def tasks_scope(self) -> str:
        """The Google Tasks scope this server asks for."""
        return SCOPE_TASKS_READONLY if self.readonly else SCOPE_TASKS_FULL

    @property
    def required_scopes(self) -> list[str]:
        """Scopes the ``oauth-proxy`` flow requests from Google."""
        return [SCOPE_OPENID, SCOPE_USERINFO_EMAIL, self.tasks_scope]

    @property
    def accepted_tasks_scopes(self) -> frozenset[str]:
        """Tasks scopes that satisfy ``resource-server`` mode.

        The full scope always works; the read-only one only when writes are off.
        """
        if self.readonly:
            return frozenset({SCOPE_TASKS_FULL, SCOPE_TASKS_READONLY})
        return frozenset({SCOPE_TASKS_FULL})


def _first(env: Mapping[str, str], *names: str) -> str | None:
    """Return the first non-blank value among ``names``, stripped."""
    for name in names:
        value = env.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _secret(env: Mapping[str, str], *names: str) -> SecretStr | None:
    """Return the first non-blank value among ``names`` as a secret."""
    value = _first(env, *names)
    return SecretStr(value) if value is not None else None


def _choice[T](raw: str, choices: Mapping[str, T], what: str) -> T:
    """Narrow ``raw`` to one of ``choices`` or raise with the accepted values."""
    value = choices.get(raw.strip().lower())
    if value is None:
        accepted = ", ".join(repr(c) for c in choices)
        msg = f"Unsupported {what} {raw!r}; expected one of {accepted}."
        raise ConfigurationError(msg)
    return value


def _bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    """Parse a boolean variable, rejecting typos instead of guessing."""
    raw = _first(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    msg = f"{name} must be one of true/false/1/0/yes/no/on/off, got {raw!r}."
    raise ConfigurationError(msg)


def _int(raw: str, name: str) -> int:
    """Parse an integer variable."""
    try:
        return int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}."
        raise ConfigurationError(msg) from exc


def _csv(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated list, dropping blanks."""
    if raw is None:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _check_base_url(url: str) -> str:
    """Require an absolute http(s) URL and drop any trailing slash."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        msg = f"MCP_BASE_URL must be an absolute http:// or https:// URL, got {url!r}."
        raise ConfigurationError(msg)
    return url.rstrip("/")


def _check_fernet(key: SecretStr | None) -> None:
    """Fail at startup, not at first login, when the storage key is malformed."""
    if key is None:
        return
    try:
        Fernet(key.get_secret_value().encode("utf-8"))
    except ValueError as exc:
        msg = (
            "STORAGE_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64-encoded "
            'bytes). Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
        raise ConfigurationError(msg) from exc


def load_settings(
    env: Mapping[str, str],
    *,
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
    auth_mode: str | None = None,
) -> Settings:
    """Build validated settings from environment variables and CLI overrides.

    Args:
        env: Environment variables (the CLI passes ``os.environ``).
        transport: Overrides ``MCP_TRANSPORT``.
        host: Overrides ``MCP_HOST``.
        port: Overrides ``MCP_PORT``.
        auth_mode: Overrides ``MCP_AUTH_MODE``.

    Returns:
        The settings.

    Raises:
        ConfigurationError: A value is invalid or a mode is missing what it needs.
    """
    resolved_transport = _choice(
        transport or _first(env, "MCP_TRANSPORT") or "http", _TRANSPORTS, "transport"
    )
    default_mode: AuthMode = "token" if resolved_transport == "stdio" else "oauth-proxy"
    resolved_mode = _choice(
        auth_mode or _first(env, "MCP_AUTH_MODE") or default_mode, _AUTH_MODES, "MCP_AUTH_MODE"
    )
    if resolved_mode != "token" and resolved_transport != "http":
        msg = (
            f"MCP_AUTH_MODE={resolved_mode!r} needs the http transport; use "
            "MCP_AUTH_MODE=token with stdio, or set MCP_TRANSPORT=http."
        )
        raise ConfigurationError(msg)

    resolved_host = host or _first(env, "MCP_HOST") or DEFAULT_HOST
    if port is None:
        raw_port = _first(env, "MCP_PORT")
        port = DEFAULT_PORT if raw_port is None else _int(raw_port, "MCP_PORT")
    if not 1 <= port <= 65535:
        msg = f"MCP_PORT must be between 1 and 65535, got {port}."
        raise ConfigurationError(msg)

    base_url = _check_base_url(
        _first(env, "MCP_BASE_URL", "FASTMCP_SERVER_AUTH_GOOGLE_BASE_URL")
        or f"http://localhost:{port}"
    )
    redirect_path = (
        _first(env, "GOOGLE_OAUTH_REDIRECT_PATH", "FASTMCP_SERVER_AUTH_GOOGLE_REDIRECT_PATH")
        or DEFAULT_REDIRECT_PATH
    )
    if not redirect_path.startswith("/"):
        redirect_path = f"/{redirect_path}"

    client_id = _first(env, "GOOGLE_OAUTH_CLIENT_ID", "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID")
    client_secret = _secret(
        env, "GOOGLE_OAUTH_CLIENT_SECRET", "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET"
    )
    audiences = _csv(_first(env, "GOOGLE_OAUTH_AUDIENCE")) or ((client_id,) if client_id else ())
    redirect_uris = _csv(_first(env, "MCP_ALLOWED_CLIENT_REDIRECT_URIS"))

    raw_ttl = _first(env, "TOKEN_CACHE_TTL_SECONDS")
    ttl = (
        DEFAULT_TOKEN_CACHE_TTL_SECONDS
        if raw_ttl is None
        else _int(raw_ttl, "TOKEN_CACHE_TTL_SECONDS")
    )
    if ttl < 0:
        msg = f"TOKEN_CACHE_TTL_SECONDS must be 0 or more, got {ttl}."
        raise ConfigurationError(msg)

    storage_backend = _choice(
        _first(env, "TOKEN_STORAGE_BACKEND") or "default",
        _STORAGE_BACKENDS,
        "TOKEN_STORAGE_BACKEND",
    )
    storage_encryption_key = _secret(env, "STORAGE_ENCRYPTION_KEY")
    _check_fernet(storage_encryption_key)

    settings = Settings(
        transport=resolved_transport,
        auth_mode=resolved_mode,
        host=resolved_host,
        port=port,
        base_url=base_url,
        readonly=_bool(env, "GOOGLE_TASKS_READONLY", default=False),
        log_level=_choice(_first(env, "MCP_LOG_LEVEL") or "info", _LOG_LEVELS, "MCP_LOG_LEVEL"),
        client_id=client_id,
        client_secret=client_secret,
        redirect_path=redirect_path,
        allowed_client_redirect_uris=redirect_uris or None,
        audiences=audiences,
        token_cache_ttl_seconds=ttl,
        jwt_signing_key=_secret(env, "JWT_SIGNING_KEY"),
        storage_backend=storage_backend,
        storage_encryption_key=storage_encryption_key,
        storage_dir=Path(_first(env, "TOKEN_STORAGE_DIR") or DEFAULT_STORAGE_DIR),
        redis_url=_secret(env, "REDIS_URL"),
        storage_key_prefix=_first(env, "STORAGE_KEY_PREFIX"),
        access_token=_secret(env, "GOOGLE_OAUTH_ACCESS_TOKEN"),
        refresh_token=_secret(env, "GOOGLE_OAUTH_REFRESH_TOKEN"),
        mcp_auth_token=_secret(env, "MCP_AUTH_TOKEN"),
    )
    match settings.auth_mode:
        case "oauth-proxy":
            _check_oauth_proxy(settings)
        case "resource-server":
            _check_resource_server(settings)
        case "token":  # pragma: no branch - exhaustive over AuthMode; no fall-through
            _check_token(settings)
    return settings


def _check_oauth_proxy(settings: Settings) -> None:
    """Validate what the OAuth proxy needs: client credentials and storage."""
    if settings.client_id is None or settings.client_secret is None:
        msg = (
            "MCP_AUTH_MODE=oauth-proxy needs Google OAuth client credentials. Set "
            "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET (or "
            "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID and FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET)."
        )
        raise ConfigurationError(msg)
    backend = settings.storage_backend
    if backend == "default":
        return
    if settings.storage_encryption_key is None:
        msg = (
            f"TOKEN_STORAGE_BACKEND={backend!r} requires STORAGE_ENCRYPTION_KEY (a valid "
            "Fernet key) so upstream Google tokens are encrypted at rest."
        )
        raise ConfigurationError(msg)
    if settings.jwt_signing_key is None:
        msg = (
            f"TOKEN_STORAGE_BACKEND={backend!r} requires JWT_SIGNING_KEY so issued MCP "
            "JWTs remain valid across restarts."
        )
        raise ConfigurationError(msg)
    if backend == "redis" and settings.redis_url is None:
        msg = "TOKEN_STORAGE_BACKEND='redis' requires REDIS_URL."
        raise ConfigurationError(msg)


def _check_resource_server(settings: Settings) -> None:
    """Require an audience so tokens minted for other OAuth apps are rejected."""
    if not settings.audiences:
        msg = (
            "MCP_AUTH_MODE=resource-server needs the OAuth client ID(s) whose tokens are "
            "accepted. Set GOOGLE_OAUTH_CLIENT_ID, or GOOGLE_OAUTH_AUDIENCE as a "
            "comma-separated list; without it, a token minted for any other app would "
            "be accepted."
        )
        raise ConfigurationError(msg)


def _check_token(settings: Settings) -> None:
    """Require a usable single-user credential."""
    can_refresh = (
        settings.refresh_token is not None
        and settings.client_id is not None
        and settings.client_secret is not None
    )
    if settings.access_token is None and not can_refresh:
        msg = (
            "MCP_AUTH_MODE=token needs GOOGLE_OAUTH_ACCESS_TOKEN, or "
            "GOOGLE_OAUTH_REFRESH_TOKEN together with GOOGLE_OAUTH_CLIENT_ID and "
            "GOOGLE_OAUTH_CLIENT_SECRET."
        )
        raise ConfigurationError(msg)
