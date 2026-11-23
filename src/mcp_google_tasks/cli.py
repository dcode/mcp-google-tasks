"""Command-line entry point: load settings and run the MCP server."""

import os
import sys
from collections.abc import Sequence
from typing import Annotated, Literal

from cyclopts import App, Parameter

from mcp_google_tasks import __version__
from mcp_google_tasks.config import load_settings
from mcp_google_tasks.errors import ConfigurationError
from mcp_google_tasks.logs import LogLevel, configure_logging
from mcp_google_tasks.server import build_server

MCP_PATH = "/mcp"
EXIT_CONFIG_ERROR = 2

type TransportFlag = Literal["http", "streamable-http", "stdio"]
type AuthModeFlag = Literal["oauth-proxy", "resource-server", "token"]

app = App(
    name="mcp-google-tasks",
    help="Google Tasks MCP server for Obot: multi-user OAuth or single-user tokens.",
    version=__version__,
)


@app.default
def serve(
    *,
    transport: Annotated[
        TransportFlag | None, Parameter(help="Transport; overrides MCP_TRANSPORT (default http).")
    ] = None,
    host: Annotated[str | None, Parameter(help="HTTP bind host; overrides MCP_HOST.")] = None,
    port: Annotated[int | None, Parameter(help="HTTP bind port; overrides MCP_PORT.")] = None,
    auth_mode: Annotated[
        AuthModeFlag | None,
        Parameter(help="Auth mode; overrides MCP_AUTH_MODE (default: token for stdio)."),
    ] = None,
    log_level: Annotated[
        LogLevel | None, Parameter(help="Lowest log level on stderr; overrides MCP_LOG_LEVEL.")
    ] = None,
) -> int:
    """Run the Google Tasks MCP server.

    Configuration comes from environment variables; flags override them.

    Args:
        transport: Transport; overrides ``MCP_TRANSPORT``.
        host: HTTP bind host; overrides ``MCP_HOST``.
        port: HTTP bind port; overrides ``MCP_PORT``.
        auth_mode: Auth mode; overrides ``MCP_AUTH_MODE``.
        log_level: Lowest log level on stderr; overrides ``MCP_LOG_LEVEL``.

    Returns:
        The process exit status: 0 on a clean stop, 2 on a configuration error.
    """
    try:
        settings = load_settings(
            os.environ, transport=transport, host=host, port=port, auth_mode=auth_mode
        )
        configure_logging(log_level or settings.log_level)
        server = build_server(settings)
    except ConfigurationError as exc:
        print(f"mcp-google-tasks: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if settings.transport == "stdio":
        server.run(transport="stdio", show_banner=False)
    else:
        server.run(
            transport="http",
            show_banner=False,
            host=settings.host,
            port=settings.port,
            path=MCP_PATH,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command line and exit with its status.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv[1:]``.
    """
    app(sys.argv[1:] if argv is None else list(argv))
