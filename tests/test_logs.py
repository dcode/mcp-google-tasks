"""Tests for structured logging: stderr only, redaction, JSON vs console output."""

import json
import logging
from typing import Any

import pytest
import structlog

from mcp_google_tasks.logs import REDACTED, LogLevel, configure_logging, redact_secrets


def _redact(**fields: Any) -> dict[str, Any]:
    """Run the redaction processor over an event."""
    return dict(redact_secrets(None, "info", {"event": "something happened", **fields}))


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "Authorization",
        "password",
        "api_key",
        "MCP_AUTH_TOKEN",
    ],
)
def test_secret_like_fields_are_redacted(key: str) -> None:
    assert _redact(**{key: "hunter2"})[key] == REDACTED


def test_ordinary_fields_and_event_are_kept() -> None:
    event = _redact(tasklist_id="abc", count=3, ok=True, nothing=None)

    assert event == {
        "event": "something happened",
        "tasklist_id": "abc",
        "count": 3,
        "ok": True,
        "nothing": None,
    }


def test_event_message_is_never_redacted() -> None:
    event = redact_secrets(None, "info", {"event": "token refreshed"})

    assert event == {"event": "token refreshed"}


def test_nested_mappings_and_sequences_are_redacted() -> None:
    event = _redact(
        request={"headers": {"Authorization": "Bearer x", "accept": "json"}, "url": "/u"},
        attempts=({"refresh_token": "r", "n": 1}, "plain"),
        other=[[{"password": "p"}]],
        labels={1: "one"},
    )

    assert event["request"] == {
        "headers": {"Authorization": REDACTED, "accept": "json"},
        "url": "/u",
    }
    assert event["attempts"] == [{"refresh_token": REDACTED, "n": 1}, "plain"]
    assert event["other"] == [[{"password": REDACTED}]]
    assert event["labels"] == {1: "one"}


def test_logs_are_json_on_stderr_when_not_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")

    structlog.get_logger().info("listed tasks", count=2, access_token="secret-value")  # noqa: S106 - test placeholder

    captured = capsys.readouterr()
    assert captured.out == ""
    record = json.loads(captured.err.strip())
    assert record["event"] == "listed tasks"
    assert record["count"] == 2
    assert record["access_token"] == REDACTED
    assert record["level"] == "info"
    assert "timestamp" in record
    assert "secret-value" not in captured.err


def test_logs_use_console_renderer_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    configure_logging("info")

    structlog.get_logger().info("listed tasks", client_secret="secret-value")  # noqa: S106 - test placeholder

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "listed tasks" in captured.err
    assert REDACTED in captured.err
    assert "secret-value" not in captured.err
    with pytest.raises(json.JSONDecodeError, match="Expecting value"):
        json.loads(captured.err.strip())


@pytest.mark.parametrize(
    ("level", "emitted", "suppressed"),
    [
        ("debug", "debug", None),
        ("info", "info", "debug"),
        ("warning", "warning", "info"),
        ("error", "error", "warning"),
    ],
)
def test_level_filters_lower_events(
    capsys: pytest.CaptureFixture[str], level: LogLevel, emitted: str, suppressed: str | None
) -> None:
    configure_logging(level)
    logger = structlog.get_logger()

    getattr(logger, emitted)("kept")
    if suppressed is not None:
        getattr(logger, suppressed)("dropped")

    err = capsys.readouterr().err
    assert "kept" in err
    assert "dropped" not in err


def test_stdlib_logging_goes_to_stderr_and_noisy_libraries_are_quieted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")

    logging.getLogger("mcp_google_tasks.example").debug("stdlib line")
    logging.getLogger("httpx").info("GET https://tasks.googleapis.com/")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mcp_google_tasks.example: stdlib line" in captured.err
    assert "googleapis" not in captured.err
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("mcp").level == logging.WARNING


def test_noisy_libraries_follow_a_stricter_level() -> None:
    configure_logging("error")

    assert logging.getLogger("httpcore").level == logging.ERROR
