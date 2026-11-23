"""Tests for settings parsing (config.py) and the error types (errors.py)."""

import dataclasses
from collections.abc import Mapping
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from mcp_google_tasks.config import (
    SCOPE_OPENID,
    SCOPE_TASKS_FULL,
    SCOPE_TASKS_READONLY,
    SCOPE_USERINFO_EMAIL,
    load_settings,
)
from mcp_google_tasks.errors import (
    ApiError,
    AuthenticationError,
    ConfigurationError,
    ConnectivityError,
    GoogleTasksError,
    InvalidArgumentError,
)

from .conftest import (
    ACCESS_TOKEN,
    BASE_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    MCP_AUTH_TOKEN,
    OAUTH_PROXY_ENV,
    REFRESH_TOKEN,
    RESOURCE_SERVER_ENV,
    TOKEN_ENV,
    SettingsFactory,
)

FERNET_KEY = Fernet.generate_key().decode()
JWT_KEY = "test-jwt-signing-key"
REDIS_URL = "redis://:test-password@redis.example.com:6379/0"
REDIS_ENV: Mapping[str, str] = {
    "TOKEN_STORAGE_BACKEND": "redis",
    "STORAGE_ENCRYPTION_KEY": FERNET_KEY,
    "JWT_SIGNING_KEY": JWT_KEY,
    "REDIS_URL": REDIS_URL,
}
CLIENT_ENV: Mapping[str, str] = {
    "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
    "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
}


def _env(base: Mapping[str, str], **overrides: str | None) -> dict[str, str]:
    """Merge ``overrides`` into ``base``; a ``None`` value removes that key."""
    merged: dict[str, str | None] = {**base, **overrides}
    return {k: v for k, v in merged.items() if v is not None}


# --- defaults and transport / auth mode -------------------------------------


def test_http_defaults_to_oauth_proxy_with_documented_defaults() -> None:
    settings = load_settings(CLIENT_ENV)

    assert settings.transport == "http"
    assert settings.auth_mode == "oauth-proxy"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.base_url == "http://localhost:8000"
    assert settings.readonly is False
    assert settings.log_level == "info"
    assert settings.client_id == CLIENT_ID
    assert settings.client_secret is not None
    assert settings.client_secret.get_secret_value() == CLIENT_SECRET
    assert settings.redirect_path == "/auth/callback"
    assert settings.allowed_client_redirect_uris is None
    assert settings.audiences == (CLIENT_ID,)
    assert settings.token_cache_ttl_seconds == 300
    assert settings.jwt_signing_key is None
    assert settings.storage_backend == "default"
    assert settings.storage_encryption_key is None
    assert settings.storage_dir == Path("/data/oauth-tokens")
    assert settings.redis_url is None
    assert settings.storage_key_prefix is None
    assert settings.access_token is None
    assert settings.refresh_token is None
    assert settings.mcp_auth_token is None


def test_stdio_defaults_to_token_mode() -> None:
    settings = load_settings({"MCP_TRANSPORT": "stdio", "GOOGLE_OAUTH_ACCESS_TOKEN": ACCESS_TOKEN})

    assert settings.transport == "stdio"
    assert settings.auth_mode == "token"
    assert settings.access_token is not None
    assert settings.access_token.get_secret_value() == ACCESS_TOKEN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http", "http"),
        ("streamable-http", "http"),
        ("  Streamable-HTTP ", "http"),
        ("STDIO", "stdio"),
    ],
)
def test_transport_names_are_normalized(raw: str, expected: str) -> None:
    settings = load_settings({**TOKEN_ENV, "MCP_TRANSPORT": raw})

    assert settings.transport == expected


def test_blank_variables_count_as_unset() -> None:
    settings = load_settings({**CLIENT_ENV, "MCP_TRANSPORT": "   ", "MCP_HOST": ""})

    assert settings.transport == "http"
    assert settings.host == "127.0.0.1"


def test_cli_overrides_beat_environment() -> None:
    env = _env(
        TOKEN_ENV,
        MCP_TRANSPORT="stdio",
        MCP_AUTH_MODE="oauth-proxy",
        MCP_HOST="192.0.2.1",
        MCP_PORT="9000",
    )

    settings = load_settings(
        env, transport="http", host="198.51.100.7", port=9100, auth_mode="token"
    )

    assert settings.transport == "http"
    assert settings.auth_mode == "token"
    assert settings.host == "198.51.100.7"
    assert settings.port == 9100
    assert settings.base_url == "http://localhost:9100"


def test_environment_sets_host_port_and_mode() -> None:
    env = _env(RESOURCE_SERVER_ENV, MCP_HOST="192.0.2.1", MCP_PORT=" 9000 ")

    settings = load_settings(env)

    assert settings.auth_mode == "resource-server"
    assert settings.host == "192.0.2.1"
    assert settings.port == 9000


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"MCP_TRANSPORT": "sse"}, r"Unsupported transport 'sse'; expected one of 'http'"),
        ({"MCP_AUTH_MODE": "basic"}, r"Unsupported MCP_AUTH_MODE 'basic'"),
        ({"MCP_LOG_LEVEL": "verbose"}, r"Unsupported MCP_LOG_LEVEL 'verbose'.*'debug'"),
        ({"TOKEN_STORAGE_BACKEND": "s3"}, r"Unsupported TOKEN_STORAGE_BACKEND 's3'"),
    ],
)
def test_unknown_choices_are_rejected_with_accepted_values(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_settings({**TOKEN_ENV, **overrides})


@pytest.mark.parametrize("mode", ["oauth-proxy", "resource-server"])
def test_multi_user_modes_need_http(mode: str) -> None:
    with pytest.raises(ConfigurationError, match=rf"MCP_AUTH_MODE='{mode}' needs the http"):
        load_settings({**OAUTH_PROXY_ENV, "MCP_TRANSPORT": "stdio", "MCP_AUTH_MODE": mode})


@pytest.mark.parametrize("level", ["debug", "info", "WARNING", "error"])
def test_log_level_is_parsed(level: str) -> None:
    settings = load_settings({**TOKEN_ENV, "MCP_LOG_LEVEL": level})

    assert settings.log_level == level.lower()


# --- port -------------------------------------------------------------------


def test_non_integer_port_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match=r"MCP_PORT must be an integer, got 'abc'"):
        load_settings({**TOKEN_ENV, "MCP_PORT": "abc"})


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_out_of_range_port_from_env_is_rejected(port: str) -> None:
    with pytest.raises(ConfigurationError, match=r"MCP_PORT must be between 1 and 65535"):
        load_settings({**TOKEN_ENV, "MCP_PORT": port})


def test_out_of_range_port_override_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match=r"between 1 and 65535, got 99999"):
        load_settings(TOKEN_ENV, port=99999)


@pytest.mark.parametrize("port", [1, 65535])
def test_port_bounds_are_inclusive(port: int) -> None:
    assert load_settings(TOKEN_ENV, port=port).port == port


# --- base URL and redirect path ---------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"MCP_BASE_URL": f"{BASE_URL}/"}, BASE_URL),
        ({"MCP_BASE_URL": "http://localhost:8000/mcp//"}, "http://localhost:8000/mcp"),
        (
            {"MCP_BASE_URL": " ", "FASTMCP_SERVER_AUTH_GOOGLE_BASE_URL": "https://alt.example.com"},
            "https://alt.example.com",
        ),
    ],
)
def test_base_url_is_normalized_with_fallback(overrides: dict[str, str], expected: str) -> None:
    settings = load_settings({**CLIENT_ENV, **overrides})

    assert settings.base_url == expected


@pytest.mark.parametrize("url", ["ftp://mcp.example.com", "mcp.example.com", "https://", "/mcp"])
def test_base_url_must_be_absolute_http(url: str) -> None:
    with pytest.raises(ConfigurationError, match=r"MCP_BASE_URL must be an absolute http"):
        load_settings({**CLIENT_ENV, "MCP_BASE_URL": url})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"GOOGLE_OAUTH_REDIRECT_PATH": "oauth/done"}, "/oauth/done"),
        ({"GOOGLE_OAUTH_REDIRECT_PATH": "/cb"}, "/cb"),
        ({"FASTMCP_SERVER_AUTH_GOOGLE_REDIRECT_PATH": "legacy/cb"}, "/legacy/cb"),
    ],
)
def test_redirect_path_gets_leading_slash(overrides: dict[str, str], expected: str) -> None:
    settings = load_settings({**CLIENT_ENV, **overrides})

    assert settings.redirect_path == expected


# --- client credentials, audiences, redirect URIs ---------------------------


def test_legacy_fastmcp_client_variables_are_accepted() -> None:
    settings = load_settings(
        {
            "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID": CLIENT_ID,
            "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        }
    )

    assert settings.client_id == CLIENT_ID
    assert settings.client_secret is not None
    assert settings.client_secret.get_secret_value() == CLIENT_SECRET


def test_primary_client_variables_win_over_legacy() -> None:
    settings = load_settings(
        {**CLIENT_ENV, "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID": "legacy.apps.example.com"}
    )

    assert settings.client_id == CLIENT_ID


@pytest.mark.parametrize(
    ("audience", "expected"),
    [
        ("a.apps.example.com, ,b.apps.example.com,", ("a.apps.example.com", "b.apps.example.com")),
        (" , ", (CLIENT_ID,)),
    ],
)
def test_audiences_parse_csv_and_fall_back_to_client_id(
    make_settings: SettingsFactory, audience: str, expected: tuple[str, ...]
) -> None:
    settings = make_settings("resource-server", GOOGLE_OAUTH_AUDIENCE=audience)

    assert settings.audiences == expected


def test_resource_server_accepts_audience_without_client_id(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings(
        "resource-server", GOOGLE_OAUTH_CLIENT_ID=None, GOOGLE_OAUTH_AUDIENCE="a.apps.example.com"
    )

    assert settings.client_id is None
    assert settings.audiences == ("a.apps.example.com",)


def test_resource_server_needs_an_audience() -> None:
    env = _env(RESOURCE_SERVER_ENV, GOOGLE_OAUTH_CLIENT_ID=None)

    with pytest.raises(ConfigurationError, match=r"resource-server needs the OAuth client ID"):
        load_settings(env)


def test_allowed_client_redirect_uris_parse_csv(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        "oauth-proxy",
        MCP_ALLOWED_CLIENT_REDIRECT_URIS="http://localhost:*, https://app.example.com/cb ,",
    )

    assert settings.allowed_client_redirect_uris == (
        "http://localhost:*",
        "https://app.example.com/cb",
    )


def test_empty_allowed_client_redirect_uris_allow_any(make_settings: SettingsFactory) -> None:
    settings = make_settings("oauth-proxy", MCP_ALLOWED_CLIENT_REDIRECT_URIS=",")

    assert settings.allowed_client_redirect_uris is None


# --- token cache TTL --------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("60", 60)])
def test_token_cache_ttl_is_parsed(make_settings: SettingsFactory, raw: str, expected: int) -> None:
    settings = make_settings("resource-server", TOKEN_CACHE_TTL_SECONDS=raw)

    assert settings.token_cache_ttl_seconds == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("-1", r"TOKEN_CACHE_TTL_SECONDS must be 0 or more, got -1"),
        ("5m", r"TOKEN_CACHE_TTL_SECONDS must be an integer, got '5m'"),
    ],
)
def test_bad_token_cache_ttl_is_rejected(raw: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_settings({**RESOURCE_SERVER_ENV, "TOKEN_CACHE_TTL_SECONDS": raw})


# --- readonly ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        (" on ", True),
        ("false", False),
        ("0", False),
        ("No", False),
        ("off", False),
        ("  ", False),
    ],
)
def test_readonly_accepts_boolean_words(raw: str, *, expected: bool) -> None:
    settings = load_settings({**TOKEN_ENV, "GOOGLE_TASKS_READONLY": raw})

    assert settings.readonly is expected


@pytest.mark.parametrize("raw", ["ture", "2", "enabled"])
def test_readonly_rejects_typos(raw: str) -> None:
    with pytest.raises(
        ConfigurationError, match=rf"GOOGLE_TASKS_READONLY must be one of .*{raw!r}"
    ):
        load_settings({**TOKEN_ENV, "GOOGLE_TASKS_READONLY": raw})


def test_full_access_scopes(make_settings: SettingsFactory) -> None:
    settings = make_settings("token")

    assert settings.tasks_scope == SCOPE_TASKS_FULL
    assert settings.required_scopes == [SCOPE_OPENID, SCOPE_USERINFO_EMAIL, SCOPE_TASKS_FULL]
    assert settings.accepted_tasks_scopes == frozenset({SCOPE_TASKS_FULL})


def test_readonly_scopes(make_settings: SettingsFactory) -> None:
    settings = make_settings("token", GOOGLE_TASKS_READONLY="true")

    assert settings.tasks_scope == SCOPE_TASKS_READONLY
    assert settings.required_scopes == [
        SCOPE_OPENID,
        SCOPE_USERINFO_EMAIL,
        SCOPE_TASKS_READONLY,
    ]
    assert settings.accepted_tasks_scopes == frozenset({SCOPE_TASKS_FULL, SCOPE_TASKS_READONLY})


# --- oauth-proxy storage ----------------------------------------------------


@pytest.mark.parametrize("missing", ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"])
def test_oauth_proxy_needs_client_credentials(missing: str) -> None:
    env = _env(OAUTH_PROXY_ENV, **{missing: None})

    with pytest.raises(ConfigurationError, match=r"oauth-proxy needs Google OAuth client"):
        load_settings(env)


def test_invalid_fernet_key_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match=r"STORAGE_ENCRYPTION_KEY is not a valid Fernet"):
        load_settings({**OAUTH_PROXY_ENV, "STORAGE_ENCRYPTION_KEY": "not-a-fernet-key"})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"TOKEN_STORAGE_BACKEND": "filetree", "JWT_SIGNING_KEY": JWT_KEY},
            r"TOKEN_STORAGE_BACKEND='filetree' requires STORAGE_ENCRYPTION_KEY",
        ),
        (
            {"TOKEN_STORAGE_BACKEND": "filetree", "STORAGE_ENCRYPTION_KEY": FERNET_KEY},
            r"TOKEN_STORAGE_BACKEND='filetree' requires JWT_SIGNING_KEY",
        ),
        (
            {
                "TOKEN_STORAGE_BACKEND": "redis",
                "STORAGE_ENCRYPTION_KEY": FERNET_KEY,
                "JWT_SIGNING_KEY": JWT_KEY,
            },
            r"TOKEN_STORAGE_BACKEND='redis' requires REDIS_URL",
        ),
    ],
)
def test_persistent_storage_requirements(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_settings({**OAUTH_PROXY_ENV, **overrides})


def test_filetree_storage_is_configured(make_settings: SettingsFactory) -> None:
    settings = load_settings(
        {
            **OAUTH_PROXY_ENV,
            "TOKEN_STORAGE_BACKEND": "FileTree",
            "STORAGE_ENCRYPTION_KEY": FERNET_KEY,
            "JWT_SIGNING_KEY": JWT_KEY,
            "TOKEN_STORAGE_DIR": "/var/lib/mcp-tokens",
            "STORAGE_KEY_PREFIX": "tasks",
        }
    )

    assert settings.storage_backend == "filetree"
    assert settings.storage_dir == Path("/var/lib/mcp-tokens")
    assert settings.storage_key_prefix == "tasks"
    assert settings.storage_encryption_key is not None
    assert settings.storage_encryption_key.get_secret_value() == FERNET_KEY
    assert settings.jwt_signing_key is not None
    assert settings.jwt_signing_key.get_secret_value() == JWT_KEY


def test_redis_storage_is_configured(make_settings: SettingsFactory) -> None:
    settings = load_settings({**OAUTH_PROXY_ENV, **REDIS_ENV})

    assert settings.storage_backend == "redis"
    assert settings.redis_url is not None
    assert settings.redis_url.get_secret_value() == REDIS_URL


# --- token mode -------------------------------------------------------------


@pytest.mark.parametrize(
    "removed",
    [
        ("GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_OAUTH_REFRESH_TOKEN"),
        ("GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_OAUTH_CLIENT_ID"),
        ("GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_OAUTH_CLIENT_SECRET"),
    ],
)
def test_token_mode_needs_a_usable_credential(removed: tuple[str, ...]) -> None:
    env = _env(TOKEN_ENV, **dict.fromkeys(removed))

    with pytest.raises(ConfigurationError, match=r"MCP_AUTH_MODE=token needs"):
        load_settings(env)


def test_token_mode_with_access_token_only(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        "token",
        GOOGLE_OAUTH_REFRESH_TOKEN=None,
        GOOGLE_OAUTH_CLIENT_ID=None,
        GOOGLE_OAUTH_CLIENT_SECRET=None,
    )

    assert settings.access_token is not None
    assert settings.refresh_token is None
    assert settings.client_id is None


def test_token_mode_with_refresh_token_only(make_settings: SettingsFactory) -> None:
    settings = make_settings("token", GOOGLE_OAUTH_ACCESS_TOKEN=None)

    assert settings.access_token is None
    assert settings.refresh_token is not None
    assert settings.refresh_token.get_secret_value() == REFRESH_TOKEN


def test_token_mode_over_http_reads_shared_secret(make_settings: SettingsFactory) -> None:
    settings = make_settings("token", MCP_TRANSPORT="http", MCP_AUTH_TOKEN=MCP_AUTH_TOKEN)

    assert settings.transport == "http"
    assert settings.auth_mode == "token"
    assert settings.mcp_auth_token is not None
    assert settings.mcp_auth_token.get_secret_value() == MCP_AUTH_TOKEN


# --- secrecy and immutability -----------------------------------------------


def test_repr_never_shows_secrets(make_settings: SettingsFactory) -> None:
    settings = load_settings(
        {
            **OAUTH_PROXY_ENV,
            **REDIS_ENV,
            "GOOGLE_OAUTH_ACCESS_TOKEN": ACCESS_TOKEN,
            "GOOGLE_OAUTH_REFRESH_TOKEN": REFRESH_TOKEN,
            "MCP_AUTH_TOKEN": MCP_AUTH_TOKEN,
        }
    )

    text = repr(settings)

    for secret in (
        CLIENT_SECRET,
        FERNET_KEY,
        JWT_KEY,
        REDIS_URL,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        MCP_AUTH_TOKEN,
    ):
        assert secret not in text
    assert "**********" in text


def test_settings_are_frozen(make_settings: SettingsFactory) -> None:
    settings = make_settings("token")

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 1  # ty: ignore[invalid-assignment] - asserting immutability


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ApiError(404, "Requested entity was not found.", "NOT_FOUND"),
            "Google Tasks API error 404 (NOT_FOUND): Requested entity was not found.",
        ),
        (ApiError(500, "oops"), "Google Tasks API error 500: oops"),
        (ApiError(502, "bad gateway", ""), "Google Tasks API error 502: bad gateway"),
    ],
)
def test_api_error_message(error: ApiError, expected: str) -> None:
    assert str(error) == expected


def test_api_error_keeps_status_fields() -> None:
    error = ApiError(403, "Forbidden", "PERMISSION_DENIED")

    assert error.status_code == 403
    assert error.api_status == "PERMISSION_DENIED"
    assert ApiError(500, "oops").api_status is None


@pytest.mark.parametrize(
    "error_type",
    [ConfigurationError, AuthenticationError, ConnectivityError, InvalidArgumentError, ApiError],
)
def test_every_error_is_a_google_tasks_error(error_type: type[Exception]) -> None:
    assert issubclass(error_type, GoogleTasksError)


def test_invalid_argument_is_also_a_value_error() -> None:
    with pytest.raises(ValueError, match="bad id"):
        raise InvalidArgumentError("bad id")
