"""Assembles the FastMCP server from settings.

``build_server`` holds no module-level state: each server owns its HTTP client
(opened by the lifespan, or injected by tests), its credential source and its
auth provider, so several servers can coexist in one process.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_google_tasks import __version__
from mcp_google_tasks.api import GoogleTasksClient, TasksApi
from mcp_google_tasks.auth import build_auth_provider
from mcp_google_tasks.config import Settings
from mcp_google_tasks.credentials import (
    CredentialSource,
    HttpClientGetter,
    RequestCredentials,
    StaticCredentials,
)
from mcp_google_tasks.tools import register_resources, register_tools

SERVER_NAME = "Google Tasks MCP Server"
INSTRUCTIONS = (
    "Manage the authenticated user's Google Tasks and Task Lists. "
    "Use '@default' as the tasklist_id to reference the user's default "
    "task list. All date/time parameters use RFC 3339 timestamps "
    "(e.g. '2026-11-22T00:00:00Z')."
)
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

log = structlog.get_logger(__name__)


class _HttpClientSlot:
    """The HTTP client of one server: injected, or opened by its lifespan."""

    def __init__(self, injected: httpx.AsyncClient | None) -> None:
        self._injected = injected
        self._client: httpx.AsyncClient | None = injected

    def get(self) -> httpx.AsyncClient:
        """Return the active client.

        Raises:
            RuntimeError: The server lifespan has not started.
        """
        if self._client is None:
            msg = "HTTP client is not initialized; the server lifespan has not started."
            raise RuntimeError(msg)
        return self._client

    @asynccontextmanager
    async def lifespan(self, _server: FastMCP) -> AsyncGenerator[None]:
        """Open a pooled client for the server's lifetime unless one was injected."""
        if self._injected is not None:
            yield
            return
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            limits=HTTP_LIMITS,
            headers={"Accept": "application/json"},
        ) as client:
            self._client = client
            try:
                yield
            finally:
                self._client = None


def build_credentials(settings: Settings, http: HttpClientGetter) -> CredentialSource:
    """Choose the credential source for the auth mode.

    Args:
        settings: Validated settings.
        http: Returns the HTTP client used for token refresh (``token`` mode).

    Returns:
        ``StaticCredentials`` in ``token`` mode, otherwise ``RequestCredentials``.
    """
    if settings.auth_mode != "token":
        return RequestCredentials()
    return StaticCredentials(
        http,
        access_token=settings.access_token,
        refresh_token=settings.refresh_token,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    )


def build_server(settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> FastMCP:
    """Create the MCP server with every tool, resource and route registered.

    Args:
        settings: Validated settings.
        http_client: Client to use instead of opening one in the lifespan
            (tests pass one over ``httpx.MockTransport``). The caller closes it.

    Returns:
        The server, ready to run.
    """
    slot = _HttpClientSlot(http_client)
    credentials = build_credentials(settings, slot.get)

    def api_factory() -> TasksApi:
        return GoogleTasksClient(slot.get(), credentials)

    if (
        settings.auth_mode == "token"
        and settings.transport == "http"
        and settings.mcp_auth_token is None
    ):
        log.warning(
            "unauthenticated_http_endpoint",
            detail=(
                "token mode over HTTP without MCP_AUTH_TOKEN: anyone who can reach this port "
                "acts as the configured Google user. Set MCP_AUTH_TOKEN or bind to localhost."
            ),
        )

    server = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=build_auth_provider(settings),
        lifespan=slot.lifespan,
    )
    register_tools(
        server,
        api_factory,
        readonly=settings.readonly,
        identity="userinfo" if settings.auth_mode == "token" else "token-claims",
    )
    register_resources(server, api_factory)

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> Response:
        """Liveness probe for containers; never requires authentication."""
        return JSONResponse({"status": "ok"})

    return server
