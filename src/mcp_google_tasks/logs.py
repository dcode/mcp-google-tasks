"""Structured logging to stderr, with secrets redacted.

Over stdio the MCP protocol owns stdout, so a stray line there would corrupt
the conversation with the agent. Everything is logged to stderr: readable on a
terminal, one JSON object per line otherwise. Any event field whose name looks
like it holds a credential is replaced before rendering.
"""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

import structlog

type LogLevel = Literal["debug", "info", "warning", "error"]

REDACTED = "[REDACTED]"

# Field names that may carry a credential. Matched anywhere in the key, case-insensitively.
_SECRET_KEY = re.compile(r"token|secret|authorization|password|key", re.IGNORECASE)

# Libraries that log every request at INFO, sometimes with full URLs.
_NOISY_LIBRARIES: tuple[str, ...] = ("httpx", "httpx2", "httpcore", "mcp")


def _redact_value(value: object) -> object:
    """Redact nested mappings and sequences; leave scalars alone."""
    if isinstance(value, MutableMapping):
        return {k: REDACTED if _is_secret_key(k) else _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def _is_secret_key(key: object) -> bool:
    """Whether a field name suggests the value is a credential."""
    return isinstance(key, str) and _SECRET_KEY.search(key) is not None


def redact_secrets(
    _logger: object, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor that replaces credential-like fields with a marker.

    Args:
        _logger: The wrapped logger (unused).
        _method: The log method name (unused).
        event_dict: The event being logged.

    Returns:
        The event with secret-looking fields redacted, recursively.
    """
    for key in list(event_dict):
        if key == "event":
            continue
        if _is_secret_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def configure_logging(level: LogLevel = "info") -> None:
    """Send structured logs at or above a level to stderr.

    Args:
        level: The lowest level to emit.
    """
    number = logging.getLevelNamesMapping()[level.upper()]
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(number),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(stream=sys.stderr, level=number, format="%(name)s: %(message)s", force=True)
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(max(number, logging.WARNING))
