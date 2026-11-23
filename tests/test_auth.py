"""Tests for the auth providers of each mode (``auth.py``).

Providers are exercised through the server's real ASGI app with
``httpx.ASGITransport``: no network, no ports. ``GoogleTokenVerifier`` talks to
Google's tokeninfo endpoint over its own HTTP stack, so resource-server tests
swap it for a stub at the ``mcp_google_tasks.auth`` seam.
"""

import dataclasses
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast, override

import fastmcp
import httpx
import pytest
from cryptography.fernet import Fernet
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider
from pydantic import SecretStr
from starlette.applications import Starlette

from mcp_google_tasks import auth
from mcp_google_tasks.auth import (
    GOOGLE_AUTHORIZATION_SERVER,
    RESOURCE_NAME,
    SharedSecretVerifier,
    TasksScopeVerifier,
    build_auth_provider,
)
from mcp_google_tasks.config import (
    SCOPE_OPENID,
    SCOPE_TASKS_FULL,
    SCOPE_TASKS_READONLY,
    SCOPE_USERINFO_EMAIL,
    AuthMode,
    Settings,
)
from mcp_google_tasks.errors import ConfigurationError
from mcp_google_tasks.server import build_server

from .conftest import BASE_URL, CLIENT_ID, MCP_AUTH_TOKEN, SettingsFactory

MCP_PATH = "/mcp"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource/mcp"
AUTH_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"
GOOD_TOKEN = "good-google-token"  # noqa: S105 - test placeholder
OTHER_CLIENT_ID = "other-client-id.apps.example.com"
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


def _bearer(token: str) -> dict[str, str]:
    return {**MCP_ACCEPT, "Authorization": f"Bearer {token}"}


def _registration(redirect_uri: str) -> dict[str, Any]:
    return {
        "redirect_uris": [redirect_uri],
        "client_name": "test-client",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


@asynccontextmanager
async def _serve(settings: Settings) -> AsyncGenerator[httpx.AsyncClient]:
    """Run the server's HTTP app in-process, lifespan included."""
    app: Starlette = build_server(settings).http_app(path=MCP_PATH)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client,
    ):
        yield client


class StubVerifier(TokenVerifier):
    """Stands in for ``GoogleTokenVerifier``: accepts one token with fixed scopes."""

    def __init__(self, scopes: Iterable[str] = (SCOPE_TASKS_FULL,)) -> None:
        super().__init__()
        self.scopes = list(scopes)
        self.seen: list[str] = []

    @override
    async def verify_token(self, token: str) -> AccessToken | None:
        self.seen.append(token)
        if token != GOOD_TOKEN:
            return None
        return AccessToken(token=token, client_id=CLIENT_ID, scopes=self.scopes)


@dataclasses.dataclass
class GoogleVerifierSpy:
    """Replaces ``GoogleTokenVerifier`` to record how it is built."""

    scopes: list[str] = dataclasses.field(default_factory=lambda: [SCOPE_TASKS_FULL])
    calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def __call__(self, **kwargs: Any) -> StubVerifier:
        self.calls.append(kwargs)
        return StubVerifier(self.scopes)


@pytest.fixture
def google_verifier(monkeypatch: pytest.MonkeyPatch) -> GoogleVerifierSpy:
    spy = GoogleVerifierSpy()
    monkeypatch.setattr(auth, "GoogleTokenVerifier", spy)
    return spy


@pytest.fixture(autouse=True)
def _fastmcp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the OAuth proxy's default store out of the real user data dir."""
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path / "fastmcp-home")


# --- token mode --------------------------------------------------------------


@pytest.mark.parametrize(
    ("env"),
    [
        {},
        {"MCP_AUTH_TOKEN": MCP_AUTH_TOKEN},
        {"MCP_TRANSPORT": "http"},
    ],
    ids=["stdio", "stdio-with-secret", "http-without-secret"],
)
def test_token_mode_without_http_secret_has_no_provider(
    make_settings: SettingsFactory, env: dict[str, str]
) -> None:
    assert build_auth_provider(make_settings("token", **env)) is None


def test_token_mode_over_http_with_secret_uses_shared_secret(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings("token", MCP_TRANSPORT="http", MCP_AUTH_TOKEN=MCP_AUTH_TOKEN)

    provider = build_auth_provider(settings)

    assert isinstance(provider, SharedSecretVerifier)


async def test_shared_secret_accepts_only_the_exact_secret() -> None:
    verifier = SharedSecretVerifier(SecretStr(MCP_AUTH_TOKEN))

    accepted = await verifier.verify_token(MCP_AUTH_TOKEN)

    assert accepted is not None
    assert accepted.token == MCP_AUTH_TOKEN
    assert accepted.client_id == auth.SINGLE_USER_CLIENT_ID
    assert accepted.scopes == []


@pytest.mark.parametrize(
    "token",
    ["", "wrong", MCP_AUTH_TOKEN[:-1], MCP_AUTH_TOKEN + "x", MCP_AUTH_TOKEN.upper()],
)
async def test_shared_secret_rejects_other_values(token: str) -> None:
    assert await SharedSecretVerifier(SecretStr(MCP_AUTH_TOKEN)).verify_token(token) is None


def test_shared_secret_repr_hides_the_secret() -> None:
    assert MCP_AUTH_TOKEN not in repr(SharedSecretVerifier(SecretStr(MCP_AUTH_TOKEN)))


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        (MCP_ACCEPT, 401),
        (_bearer("wrong"), 401),
        (_bearer(MCP_AUTH_TOKEN), 200),
    ],
    ids=["missing", "wrong", "correct"],
)
async def test_shared_secret_gates_the_http_endpoint(
    make_settings: SettingsFactory, headers: dict[str, str], status: int
) -> None:
    settings = make_settings("token", MCP_TRANSPORT="http", MCP_AUTH_TOKEN=MCP_AUTH_TOKEN)

    async with _serve(settings) as client:
        response = await client.post(MCP_PATH, json=INITIALIZE, headers=headers)

    assert response.status_code == status


# --- oauth-proxy mode --------------------------------------------------------


def test_oauth_proxy_builds_google_provider(make_settings: SettingsFactory) -> None:
    assert isinstance(build_auth_provider(make_settings("oauth-proxy")), GoogleProvider)


@pytest.mark.parametrize(
    ("readonly", "tasks_scope"),
    [("false", SCOPE_TASKS_FULL), ("true", SCOPE_TASKS_READONLY)],
)
async def test_oauth_proxy_metadata_advertises_scopes(
    make_settings: SettingsFactory, readonly: str, tasks_scope: str
) -> None:
    settings = make_settings("oauth-proxy", GOOGLE_TASKS_READONLY=readonly)

    async with _serve(settings) as client:
        response = await client.get(AUTH_SERVER_METADATA_PATH)

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"].rstrip("/") == BASE_URL
    assert metadata["scopes_supported"] == [SCOPE_OPENID, SCOPE_USERINFO_EMAIL, tasks_scope]


async def test_oauth_proxy_serves_the_configured_callback_path(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings("oauth-proxy", GOOGLE_OAUTH_REDIRECT_PATH="oauth/google/done")
    app = build_server(settings).http_app(path=MCP_PATH)

    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/oauth/google/done" in paths
    assert "/auth/callback" not in paths


@pytest.mark.parametrize(
    ("redirect_uri", "status"),
    [
        ("http://localhost:3000/callback", 201),
        ("https://app.example.com/oauth/callback", 201),
        ("https://other.example.com/callback", 400),
    ],
)
async def test_oauth_proxy_limits_client_redirect_uris(
    make_settings: SettingsFactory, redirect_uri: str, status: int
) -> None:
    settings = make_settings(
        "oauth-proxy",
        MCP_ALLOWED_CLIENT_REDIRECT_URIS=(
            "http://localhost:*/callback, https://app.example.com/oauth/callback"
        ),
    )

    async with _serve(settings) as client:
        response = await client.post("/register", json=_registration(redirect_uri))

    assert response.status_code == status


async def test_oauth_proxy_allows_any_redirect_uri_by_default(
    make_settings: SettingsFactory,
) -> None:
    async with _serve(make_settings("oauth-proxy")) as client:
        response = await client.post(
            "/register", json=_registration("https://other.example.com/callback")
        )

    assert response.status_code == 201


async def test_oauth_proxy_persists_registrations_in_configured_store(
    make_settings: SettingsFactory, tmp_path: Path
) -> None:
    storage_dir = tmp_path / "tokens"
    settings = make_settings(
        "oauth-proxy",
        TOKEN_STORAGE_BACKEND="filetree",  # noqa: S106 - a backend name, not a secret
        TOKEN_STORAGE_DIR=str(storage_dir),
        STORAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        JWT_SIGNING_KEY="test-jwt-signing-key",
    )

    async with _serve(settings) as client:
        response = await client.post(
            "/register", json=_registration("http://localhost:3000/callback")
        )

    assert response.status_code == 201
    assert any(p.is_file() for p in storage_dir.rglob("*"))


@pytest.mark.parametrize("missing", ["client_id", "client_secret"])
def test_oauth_proxy_without_client_credentials_is_a_configuration_error(
    make_settings: SettingsFactory, missing: str
) -> None:
    # load_settings already rejects this; check the builder's own guard.
    settings = dataclasses.replace(make_settings("oauth-proxy"), **{missing: None})

    with pytest.raises(ConfigurationError, match="GOOGLE_OAUTH_CLIENT_ID"):
        build_auth_provider(settings)


# --- resource-server mode ----------------------------------------------------


@pytest.mark.parametrize(
    ("env", "audience"),
    [
        ({}, CLIENT_ID),
        ({"GOOGLE_OAUTH_AUDIENCE": OTHER_CLIENT_ID}, OTHER_CLIENT_ID),
        (
            {"GOOGLE_OAUTH_AUDIENCE": f"{CLIENT_ID}, {OTHER_CLIENT_ID}"},
            [CLIENT_ID, OTHER_CLIENT_ID],
        ),
    ],
    ids=["client-id", "single-audience", "several-audiences"],
)
def test_resource_server_verifies_the_configured_audience(
    make_settings: SettingsFactory,
    google_verifier: GoogleVerifierSpy,
    env: dict[str, str],
    audience: str | list[str],
) -> None:
    build_auth_provider(make_settings("resource-server", **env))

    assert [call["audience"] for call in google_verifier.calls] == [audience]


@pytest.mark.parametrize(("ttl", "cache_ttl"), [("300", 300), ("45", 45), ("0", None)])
def test_resource_server_token_cache_ttl(
    make_settings: SettingsFactory,
    google_verifier: GoogleVerifierSpy,
    ttl: str,
    cache_ttl: int | None,
) -> None:
    build_auth_provider(make_settings("resource-server", TOKEN_CACHE_TTL_SECONDS=ttl))

    assert [call["cache_ttl_seconds"] for call in google_verifier.calls] == [cache_ttl]


def test_resource_server_without_audience_is_a_configuration_error(
    make_settings: SettingsFactory,
) -> None:
    # load_settings already rejects this; check the builder's own guard.
    settings = dataclasses.replace(make_settings("resource-server"), audiences=())

    with pytest.raises(ConfigurationError, match="GOOGLE_OAUTH_AUDIENCE"):
        build_auth_provider(settings)


@pytest.mark.parametrize(
    ("readonly", "tasks_scope"),
    [("false", SCOPE_TASKS_FULL), ("true", SCOPE_TASKS_READONLY)],
)
async def test_resource_server_serves_protected_resource_metadata(
    make_settings: SettingsFactory,
    google_verifier: GoogleVerifierSpy,
    readonly: str,
    tasks_scope: str,
) -> None:
    settings = make_settings("resource-server", GOOGLE_TASKS_READONLY=readonly)

    async with _serve(settings) as client:
        response = await client.get(PROTECTED_RESOURCE_PATH)

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == f"{BASE_URL}{MCP_PATH}"
    assert [s.rstrip("/") for s in metadata["authorization_servers"]] == [
        GOOGLE_AUTHORIZATION_SERVER
    ]
    assert metadata["scopes_supported"] == [tasks_scope]
    assert metadata["resource_name"] == RESOURCE_NAME


@pytest.mark.parametrize("readonly", ["false", "true"])
async def test_resource_server_challenge_names_metadata_and_scope(
    make_settings: SettingsFactory, google_verifier: GoogleVerifierSpy, readonly: str
) -> None:
    settings = make_settings("resource-server", GOOGLE_TASKS_READONLY=readonly)

    async with _serve(settings) as client:
        response = await client.post(MCP_PATH, json=INITIALIZE, headers=MCP_ACCEPT)

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert f'scope="{settings.tasks_scope}"' in challenge
    assert f'resource_metadata="{BASE_URL}{PROTECTED_RESOURCE_PATH}"' in challenge


@pytest.mark.parametrize(
    ("readonly", "token_scopes", "token", "status"),
    [
        ("false", [SCOPE_TASKS_FULL], GOOD_TOKEN, 200),
        ("false", [SCOPE_TASKS_READONLY], GOOD_TOKEN, 401),
        ("false", [SCOPE_OPENID], GOOD_TOKEN, 401),
        ("true", [SCOPE_TASKS_READONLY], GOOD_TOKEN, 200),
        ("true", [SCOPE_TASKS_FULL], GOOD_TOKEN, 200),
        ("true", [SCOPE_TASKS_FULL], "forged-token", 401),
    ],
)
async def test_resource_server_requires_a_tasks_scope_over_http(
    make_settings: SettingsFactory,
    google_verifier: GoogleVerifierSpy,
    readonly: str,
    token_scopes: list[str],
    token: str,
    status: int,
) -> None:
    google_verifier.scopes = token_scopes
    settings = make_settings("resource-server", GOOGLE_TASKS_READONLY=readonly)

    async with _serve(settings) as client:
        response = await client.post(MCP_PATH, json=INITIALIZE, headers=_bearer(token))

    assert response.status_code == status


# --- TasksScopeVerifier ------------------------------------------------------


@pytest.mark.parametrize(
    ("token_scopes", "accepted", "allowed"),
    [
        ([SCOPE_TASKS_FULL], {SCOPE_TASKS_FULL}, True),
        ([SCOPE_OPENID, SCOPE_TASKS_FULL], {SCOPE_TASKS_FULL}, True),
        ([SCOPE_TASKS_READONLY], {SCOPE_TASKS_FULL}, False),
        ([SCOPE_TASKS_READONLY], {SCOPE_TASKS_FULL, SCOPE_TASKS_READONLY}, True),
        ([], {SCOPE_TASKS_FULL, SCOPE_TASKS_READONLY}, False),
    ],
)
async def test_scope_verifier_needs_any_accepted_scope(
    token_scopes: list[str], accepted: set[str], allowed: bool
) -> None:
    verifier = TasksScopeVerifier(StubVerifier(token_scopes), frozenset(accepted))

    result = await verifier.verify_token(GOOD_TOKEN)

    assert (result is not None) is allowed
    if result is not None:
        assert result.token == GOOD_TOKEN


async def test_scope_verifier_rejects_what_the_inner_verifier_rejects() -> None:
    inner = StubVerifier()
    verifier = TasksScopeVerifier(inner, frozenset({SCOPE_TASKS_FULL}))

    assert await verifier.verify_token("forged-token") is None
    assert inner.seen == ["forged-token"]


def test_unknown_auth_mode_fails_closed(make_settings: SettingsFactory) -> None:
    # load_settings only produces known modes; force one past it to check that
    # the builder never falls through to "no authentication".
    settings = dataclasses.replace(make_settings("token"), auth_mode=cast("AuthMode", "anonymous"))

    with pytest.raises(AssertionError, match="unreachable"):
        build_auth_provider(settings)
