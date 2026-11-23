"""Tests for the command line: version, flag parsing, config errors and run dispatch."""

import os
from collections.abc import Mapping
from typing import Any

import pytest
from fastmcp import FastMCP

from mcp_google_tasks import __version__
from mcp_google_tasks.cli import EXIT_CONFIG_ERROR, MCP_PATH, main

from .conftest import OAUTH_PROXY_ENV, TOKEN_ENV

# Prefixes of every variable the settings loader reads, cleared before each test.
_CONFIG_PREFIXES: tuple[str, ...] = (
    "MCP_",
    "GOOGLE_",
    "FASTMCP_",
    "TOKEN_",
    "JWT_",
    "STORAGE_",
    "REDIS_",
)

type RunCalls = list[dict[str, Any]]


def _use_env(monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]) -> None:
    """Replace the configuration environment with exactly ``env``."""
    for name in list(os.environ):
        if name.startswith(_CONFIG_PREFIXES):
            monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> RunCalls:
    """Record ``FastMCP.run`` calls instead of serving."""
    calls: RunCalls = []

    def fake_run(_self: FastMCP, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(FastMCP, "run", fake_run)
    return calls


def _exit_code(argv: list[str]) -> object:
    """Run the CLI and return the code it exits with."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    return excinfo.value.code


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert _exit_code(["--version"]) in {0, None}
    assert capsys.readouterr().out.strip() == __version__


def test_help_lists_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert _exit_code(["--help"]) in {0, None}
    out = capsys.readouterr().out
    for flag in ("--transport", "--host", "--port", "--auth-mode", "--log-level"):
        assert flag in out


def test_stdio_runs_without_banner(monkeypatch: pytest.MonkeyPatch, run_calls: RunCalls) -> None:
    _use_env(monkeypatch, TOKEN_ENV)

    assert _exit_code([]) == 0
    assert run_calls == [{"transport": "stdio", "show_banner": False}]


def test_http_flags_override_environment(
    monkeypatch: pytest.MonkeyPatch, run_calls: RunCalls
) -> None:
    _use_env(monkeypatch, {**TOKEN_ENV, "MCP_HOST": "127.0.0.1", "MCP_PORT": "8000"})

    code = _exit_code(
        ["--transport", "http", "--host", "0.0.0.0", "--port", "9123", "--log-level", "debug"]  # noqa: S104 - bind flag under test
    )

    assert code == 0
    assert run_calls == [
        {
            "transport": "http",
            "show_banner": False,
            "host": "0.0.0.0",  # noqa: S104 - bind flag under test
            "port": 9123,
            "path": MCP_PATH,
        }
    ]


def test_streamable_http_alias_and_auth_mode_flag(
    monkeypatch: pytest.MonkeyPatch, run_calls: RunCalls
) -> None:
    _use_env(monkeypatch, {**OAUTH_PROXY_ENV, "MCP_AUTH_MODE": "token", "MCP_TRANSPORT": "stdio"})

    assert _exit_code(["--transport", "streamable-http", "--auth-mode", "oauth-proxy"]) == 0
    assert len(run_calls) == 1
    assert run_calls[0]["transport"] == "http"
    assert run_calls[0]["path"] == MCP_PATH


@pytest.mark.parametrize(
    ("env", "argv", "message"),
    [
        pytest.param({"MCP_TRANSPORT": "stdio"}, [], "configuration error", id="missing-token"),
        pytest.param(TOKEN_ENV, ["--transport", "http", "--port", "99999"], "port", id="bad-port"),
        pytest.param(
            TOKEN_ENV,
            ["--auth-mode", "resource-server"],
            "configuration error",
            id="resource-server-over-stdio",
        ),
    ],
)
def test_configuration_error_exits_2_with_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_calls: RunCalls,
    env: Mapping[str, str],
    argv: list[str],
    message: str,
) -> None:
    _use_env(monkeypatch, env)

    assert _exit_code(argv) == EXIT_CONFIG_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("mcp-google-tasks: configuration error:")
    assert message in captured.err.lower()
    assert run_calls == []


@pytest.mark.parametrize(
    ("argv"),
    [
        pytest.param(["--transport", "carrier-pigeon"], id="bad-transport"),
        pytest.param(["--port", "not-a-number"], id="bad-port-type"),
        pytest.param(["--log-level", "verbose"], id="bad-log-level"),
    ],
)
def test_invalid_flag_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch, run_calls: RunCalls, argv: list[str]
) -> None:
    _use_env(monkeypatch, TOKEN_ENV)

    assert _exit_code(argv) not in {0, None}
    assert run_calls == []


def test_main_defaults_to_sys_argv(monkeypatch: pytest.MonkeyPatch, run_calls: RunCalls) -> None:
    _use_env(monkeypatch, TOKEN_ENV)
    monkeypatch.setattr("sys.argv", ["mcp-google-tasks", "--transport", "stdio"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    assert run_calls == [{"transport": "stdio", "show_banner": False}]
