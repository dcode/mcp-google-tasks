"""Tests for ``python -m mcp_google_tasks``."""

import runpy

import pytest

from mcp_google_tasks import __version__


def test_module_entry_point_runs_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["mcp_google_tasks", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("mcp_google_tasks", run_name="__main__", alter_sys=True)

    assert excinfo.value.code in {0, None}
    assert capsys.readouterr().out.strip() == __version__


def test_importing_module_does_not_run_cli(capsys: pytest.CaptureFixture[str]) -> None:
    runpy.run_module("mcp_google_tasks.__main__", run_name="mcp_google_tasks.__main__")

    assert capsys.readouterr().out == ""
