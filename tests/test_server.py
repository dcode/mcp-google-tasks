"""Tests for server assembly (``server.py``): credentials, lifespan, routes."""

from pathlib import Path
from typing import Any

import fastmcp
import httpx
import pytest
import structlog
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette

from mcp_google_tasks import __version__
from mcp_google_tasks.config import AuthMode, Settings
from mcp_google_tasks.credentials import RequestCredentials, StaticCredentials
from mcp_google_tasks.server import INSTRUCTIONS, SERVER_NAME, build_credentials, build_server

from .conftest import (
    ACCESS_TOKEN,
    BASE_URL,
    DEFAULT_LIST_ID,
    MCP_AUTH_TOKEN,
    USERINFO_HOST,
    FakeGoogle,
    ServerFactory,
    SettingsFactory,
    call_tool,
)

UNAUTHENTICATED_HTTP_EVENT = "unauthenticated_http_endpoint"


def _unused_http() -> httpx.AsyncClient:
    msg = "building credentials must not touch the HTTP client"
    raise AssertionError(msg)


@pytest.fixture
def owned_clients(
    fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> list[httpx.AsyncClient]:
    """Route clients the server opens itself to ``fake_google`` and record them."""
    real_async_client = httpx.AsyncClient
    opened: list[httpx.AsyncClient] = []

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        client = real_async_client(transport=fake_google.transport, **kwargs)
        opened.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return opened


# --- build_credentials -------------------------------------------------------


def test_token_mode_uses_static_credentials(token_settings: Settings) -> None:
    credentials = build_credentials(token_settings, _unused_http)

    assert isinstance(credentials, StaticCredentials)
    assert credentials.can_refresh


def test_token_mode_without_refresh_token_cannot_refresh(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings("token", GOOGLE_OAUTH_REFRESH_TOKEN=None)

    credentials = build_credentials(settings, _unused_http)

    assert isinstance(credentials, StaticCredentials)
    assert not credentials.can_refresh


@pytest.mark.parametrize("auth_mode", ["oauth-proxy", "resource-server"])
def test_multi_user_modes_use_request_credentials(
    make_settings: SettingsFactory, auth_mode: AuthMode
) -> None:
    credentials = build_credentials(make_settings(auth_mode), _unused_http)

    assert isinstance(credentials, RequestCredentials)


async def test_static_credentials_use_the_server_client(
    mcp_client: Client[Any], fake_google: FakeGoogle
) -> None:
    await call_tool(mcp_client, "list_tasklists")

    assert fake_google.last.authorization == f"Bearer {ACCESS_TOKEN}"


async def test_static_credentials_refresh_through_the_server_client(
    mcp_client: Client[Any], fake_google: FakeGoogle
) -> None:
    fake_google.expire_access_tokens()

    await call_tool(mcp_client, "list_tasklists")

    assert len(fake_google.token_requests) == 1
    assert fake_google.last.authorization == "Bearer refreshed-token-1"


# --- server identity and registration ---------------------------------------


async def test_server_identifies_itself(mcp_client: Client[Any]) -> None:
    assert mcp_client.server_info is not None
    assert mcp_client.server_info.name == SERVER_NAME
    assert mcp_client.server_info.version == __version__
    assert mcp_client.instructions == INSTRUCTIONS


@pytest.mark.parametrize(("readonly", "count"), [("false", 15), ("true", 5)])
async def test_readonly_setting_controls_registered_tools(
    make_settings: SettingsFactory, make_server: ServerFactory, readonly: str, count: int
) -> None:
    server = make_server(make_settings("token", GOOGLE_TASKS_READONLY=readonly))

    async with Client(server) as client:
        tools = await client.list_tools()

    assert len(tools) == count
    if readonly == "true":
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_resources_are_registered(mcp_client: Client[Any]) -> None:
    resources = await mcp_client.list_resources()
    templates = await mcp_client.list_resource_templates()

    assert [str(r.uri) for r in resources] == ["gtasks://lists"]
    assert [t.uri_template for t in templates] == ["gtasks://lists/{tasklist_id}/tasks"]


async def test_token_mode_identity_comes_from_userinfo(
    mcp_client: Client[Any], fake_google: FakeGoogle
) -> None:
    user = await call_tool(mcp_client, "get_current_user")

    assert user["email"] == fake_google.userinfo["email"]
    assert fake_google.last.host == USERINFO_HOST


# --- lifespan and the HTTP client -------------------------------------------


async def test_injected_client_is_used_and_left_open(
    make_server: ServerFactory, http_client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    server = make_server()

    async with Client(server) as client:
        await call_tool(client, "list_tasks", {"tasklist_id": DEFAULT_LIST_ID})

    assert fake_google.api_requests
    assert not http_client.is_closed
    async with Client(server) as client:
        await call_tool(client, "list_tasklists")


async def test_owned_client_is_opened_per_lifespan_and_closed(
    token_settings: Settings,
    owned_clients: list[httpx.AsyncClient],
    fake_google: FakeGoogle,
) -> None:
    server = build_server(token_settings)

    async with Client(server) as client:
        assert len(owned_clients) == 1
        await call_tool(client, "list_tasklists")
        assert not owned_clients[0].is_closed

    assert owned_clients[0].is_closed
    assert fake_google.api_requests


async def test_owned_client_is_reopened_and_refreshed_token_survives(
    token_settings: Settings,
    owned_clients: list[httpx.AsyncClient],
    fake_google: FakeGoogle,
) -> None:
    server = build_server(token_settings)
    fake_google.expire_access_tokens()

    async with Client(server) as client:
        await call_tool(client, "list_tasklists")
    async with Client(server) as client:
        await call_tool(client, "list_tasklists")

    assert len(owned_clients) == 2
    assert all(c.is_closed for c in owned_clients)
    assert len(fake_google.token_requests) == 1
    assert fake_google.last.authorization == "Bearer refreshed-token-1"


async def test_owned_client_sends_json_accept_header_and_timeouts(
    token_settings: Settings, owned_clients: list[httpx.AsyncClient]
) -> None:
    async with Client(build_server(token_settings)):
        (client,) = owned_clients
        assert client.headers["Accept"] == "application/json"
        assert client.timeout.connect == pytest.approx(10.0)
        assert client.timeout.read == pytest.approx(30.0)


async def test_tools_outside_the_lifespan_report_it_has_not_started(
    token_settings: Settings,
) -> None:
    server = build_server(token_settings)

    with pytest.raises(ToolError, match="lifespan has not started"):
        await server.call_tool("list_tasklists", {})


# --- /health and unauthenticated-endpoint warning ---------------------------


@pytest.mark.parametrize(
    ("auth_mode", "env"),
    [
        ("token", {"MCP_TRANSPORT": "http"}),
        ("token", {"MCP_TRANSPORT": "http", "MCP_AUTH_TOKEN": MCP_AUTH_TOKEN}),
        ("oauth-proxy", {}),
        ("resource-server", {}),
    ],
    ids=["token-open", "token-secret", "oauth-proxy", "resource-server"],
)
async def test_health_needs_no_authentication(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    auth_mode: AuthMode,
    env: dict[str, str],
) -> None:
    # Keep the OAuth proxy's default store out of the real user data dir.
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
    app: Starlette = build_server(make_settings(auth_mode, **env)).http_app(path="/mcp")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_not_an_mcp_tool_or_resource(mcp_client: Client[Any]) -> None:
    tools = {t.name for t in await mcp_client.list_tools()}
    resources = {str(r.uri) for r in await mcp_client.list_resources()}

    assert "health" not in tools
    assert not any("health" in uri for uri in resources)


def test_token_mode_over_open_http_logs_a_warning(make_settings: SettingsFactory) -> None:
    settings = make_settings("token", MCP_TRANSPORT="http")

    with structlog.testing.capture_logs() as logs:
        build_server(settings)

    warnings = [e for e in logs if e["event"] == UNAUTHENTICATED_HTTP_EVENT]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert "MCP_AUTH_TOKEN" in warnings[0]["detail"]


@pytest.mark.parametrize(
    ("auth_mode", "env"),
    [
        ("token", {}),
        ("token", {"MCP_TRANSPORT": "http", "MCP_AUTH_TOKEN": MCP_AUTH_TOKEN}),
        ("resource-server", {}),
    ],
    ids=["token-stdio", "token-secret", "resource-server"],
)
def test_no_warning_when_the_endpoint_is_protected_or_local(
    make_settings: SettingsFactory, auth_mode: AuthMode, env: dict[str, str]
) -> None:
    settings = make_settings(auth_mode, **env)

    with structlog.testing.capture_logs() as logs:
        build_server(settings)

    assert not [e for e in logs if e["event"] == UNAUTHENTICATED_HTTP_EVENT]


async def test_servers_are_independent(
    make_settings: SettingsFactory, make_server: ServerFactory
) -> None:
    full = make_server(make_settings("token"))
    readonly = make_server(make_settings("token", GOOGLE_TASKS_READONLY="true"))

    async with Client(full) as a, Client(readonly) as b:
        assert len(await a.list_tools()) == 15
        assert len(await b.list_tools()) == 5
