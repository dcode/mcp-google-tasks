"""Tests for ``mcp_google_tasks.credentials``: where the bearer token comes from."""

import asyncio
from typing import Any

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from pydantic import SecretStr
from structlog.testing import capture_logs

from mcp_google_tasks import credentials
from mcp_google_tasks.credentials import (
    EXPIRY_SKEW_SECONDS,
    GOOGLE_TOKEN_ENDPOINT,
    RequestCredentials,
    StaticCredentials,
)
from mcp_google_tasks.errors import AuthenticationError, ConnectivityError

from .conftest import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    TOKEN_HOST,
    FakeGoogle,
)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _static(
    http: httpx.AsyncClient,
    clock: FakeClock,
    *,
    access_token: str | None = ACCESS_TOKEN,
    refresh_token: str | None = REFRESH_TOKEN,
    client_id: str | None = CLIENT_ID,
    client_secret: str | None = CLIENT_SECRET,
) -> StaticCredentials:
    return StaticCredentials(
        lambda: http,
        access_token=SecretStr(access_token) if access_token is not None else None,
        refresh_token=SecretStr(refresh_token) if refresh_token is not None else None,
        client_id=client_id,
        client_secret=SecretStr(client_secret) if client_secret is not None else None,
        clock=clock,
    )


# --- RequestCredentials ------------------------------------------------------


def _access_token(token: str) -> AccessToken:
    return AccessToken(token=token, client_id=CLIENT_ID, scopes=[])


async def test_request_credentials_returns_the_request_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials, "get_access_token", lambda: _access_token("user-token"))
    assert await RequestCredentials().bearer_token() == "user-token"


@pytest.mark.parametrize("token", [None, _access_token("")])
async def test_request_credentials_without_a_token_fail(
    monkeypatch: pytest.MonkeyPatch, token: AccessToken | None
) -> None:
    monkeypatch.setattr(credentials, "get_access_token", lambda: token)
    with pytest.raises(AuthenticationError, match="No authenticated Google OAuth access token"):
        await RequestCredentials().bearer_token()


async def test_request_credentials_outside_a_request_fail() -> None:
    with pytest.raises(AuthenticationError, match="Sign in again"):
        await RequestCredentials().bearer_token()


async def test_request_credentials_never_refresh() -> None:
    assert await RequestCredentials().refresh() is False


# --- StaticCredentials: supplied token ---------------------------------------


async def test_supplied_token_is_used_without_expiry(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock)
    assert await creds.bearer_token() == ACCESS_TOKEN
    clock.now += 10 * 365 * 86400
    assert await creds.bearer_token() == ACCESS_TOKEN
    assert fake_google.requests == []


@pytest.mark.parametrize(
    ("missing", "can_refresh"),
    [
        (None, True),
        ("refresh_token", False),
        ("client_id", False),
        ("client_secret", False),
    ],
)
def test_can_refresh_needs_every_refresh_credential(
    http_client: httpx.AsyncClient, clock: FakeClock, missing: str | None, can_refresh: bool
) -> None:
    overrides: dict[str, Any] = {missing: None} if missing else {}
    creds = _static(http_client, clock, **overrides)
    assert creds.can_refresh is can_refresh
    assert repr(creds) == f"StaticCredentials(can_refresh={can_refresh})"


def test_repr_hides_every_secret(http_client: httpx.AsyncClient, clock: FakeClock) -> None:
    text = repr(_static(http_client, clock))
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in text


def test_default_clock_is_accepted(http_client: httpx.AsyncClient) -> None:
    creds = StaticCredentials(
        lambda: http_client,
        access_token=None,
        refresh_token=None,
        client_id=None,
        client_secret=None,
    )
    assert creds.can_refresh is False


# --- StaticCredentials: refresh ----------------------------------------------


async def test_refresh_posts_the_grant_and_replaces_the_token(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock)
    assert await creds.refresh() is True
    assert await creds.bearer_token() == "refreshed-token-1"
    (request,) = fake_google.token_requests
    assert (request.method, request.host, request.path) == ("POST", TOKEN_HOST, "/token")
    assert f"https://{request.host}{request.path}" == GOOGLE_TOKEN_ENDPOINT
    assert request.form == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    assert request.authorization is None


async def test_refresh_without_credentials_returns_false(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock, refresh_token=None)
    assert await creds.refresh() is False
    assert await creds.bearer_token() == ACCESS_TOKEN
    assert fake_google.requests == []


async def test_refresh_logs_without_the_token(
    http_client: httpx.AsyncClient, clock: FakeClock
) -> None:
    creds = _static(http_client, clock)
    with capture_logs() as logs:
        await creds.refresh()
    assert logs == [{"event": "google_token_refreshed", "expires_in": 3600.0, "log_level": "info"}]


async def test_missing_access_token_is_fetched_on_first_use(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock, access_token=None)
    assert await creds.bearer_token() == "refreshed-token-1"
    assert await creds.bearer_token() == "refreshed-token-1"
    assert len(fake_google.token_requests) == 1


async def test_no_token_and_no_refresh_is_an_authentication_error(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock, access_token=None, client_secret=None)
    with pytest.raises(AuthenticationError, match="no refresh token is configured"):
        await creds.bearer_token()
    assert fake_google.requests == []


@pytest.mark.parametrize(
    ("expires_in", "ttl"),
    [
        (3600, 3600.0),
        (120.5, 120.5),
        (None, credentials.DEFAULT_EXPIRES_IN),
    ],
)
async def test_refreshed_token_expires_early_by_the_skew(
    http_client: httpx.AsyncClient,
    fake_google: FakeGoogle,
    clock: FakeClock,
    expires_in: float | None,
    ttl: float,
) -> None:
    if expires_in is None:
        fake_google.refresh_expires_in = None
    else:
        fake_google.fail_next(
            200, {"access_token": "refreshed-token-1", "expires_in": expires_in}, match="/token"
        )
        fake_google.valid_tokens.add("refreshed-token-1")
    creds = _static(http_client, clock, access_token=None)
    start = clock.now
    await creds.bearer_token()

    clock.now = start + ttl - EXPIRY_SKEW_SECONDS - 0.01
    await creds.bearer_token()
    assert len(fake_google.token_requests) == 1

    clock.now = start + ttl - EXPIRY_SKEW_SECONDS
    await creds.bearer_token()
    assert len(fake_google.token_requests) == 2


@pytest.mark.parametrize("expires_in", [True, "3600", 30])
async def test_odd_or_short_expiry_never_extends_the_token(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock, expires_in: Any
) -> None:
    fake_google.fail_next(200, {"access_token": "short", "expires_in": expires_in}, match="/token")
    creds = _static(http_client, clock, access_token=None)
    assert await creds.bearer_token() == "short"
    if expires_in == 30:  # shorter than the skew: expired at once
        assert await creds.bearer_token() == "refreshed-token-1"
        assert len(fake_google.token_requests) == 2
    else:  # not a number: the default lifetime applies
        clock.now += credentials.DEFAULT_EXPIRES_IN - EXPIRY_SKEW_SECONDS - 1
        assert await creds.bearer_token() == "short"
        assert len(fake_google.token_requests) == 1


async def test_concurrent_callers_share_one_refresh(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock, access_token=None)
    tokens = await asyncio.gather(*(creds.bearer_token() for _ in range(5)))
    assert tokens == ["refreshed-token-1"] * 5
    assert len(fake_google.token_requests) == 1


async def test_refreshes_are_serialized(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    creds = _static(http_client, clock)
    results = await asyncio.gather(creds.refresh(), creds.refresh(), creds.bearer_token())
    assert results[:2] == [True, True]
    assert len(fake_google.token_requests) == 2
    assert await creds.bearer_token() == "refreshed-token-2"


# --- StaticCredentials: refresh failures -------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (400, {"error": "invalid_grant", "error_description": "Gone."}, "invalid_grant: Gone."),
        (400, {"error": "invalid_grant"}, "invalid_grant"),
        (400, {"error_description": "only a description"}, "unknown"),
        (500, "upstream exploded", "unknown"),
        (500, '["not", "an", "object"]', "unknown"),
    ],
)
async def test_token_endpoint_errors_are_authentication_errors(
    http_client: httpx.AsyncClient,
    fake_google: FakeGoogle,
    clock: FakeClock,
    status: int,
    body: dict[str, str] | str,
    reason: str,
) -> None:
    fake_google.fail_next(status, body, match="/token")
    creds = _static(http_client, clock)
    expected = rf"\(HTTP {status}, {reason}\)\. The refresh token may be revoked"
    with pytest.raises(AuthenticationError, match=expected):
        await creds.refresh()
    assert await creds.bearer_token() == ACCESS_TOKEN


async def test_revoked_refresh_token_from_google(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    fake_google.refresh_token = "other"  # noqa: S105 - test placeholder
    creds = _static(http_client, clock)
    with pytest.raises(AuthenticationError, match="invalid_grant: Token has been revoked"):
        await creds.refresh()


@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "Bearer"},
        {"access_token": ""},
        {"access_token": 12345},
        "not json",
    ],
)
async def test_success_without_access_token_is_an_authentication_error(
    http_client: httpx.AsyncClient,
    fake_google: FakeGoogle,
    clock: FakeClock,
    body: dict[str, Any] | str,
) -> None:
    fake_google.fail_next(200, body, match="/token")
    creds = _static(http_client, clock, access_token=None)
    with pytest.raises(AuthenticationError, match="answered without an access_token"):
        await creds.bearer_token()


async def test_unreachable_token_endpoint_is_a_connectivity_error(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    fake_google.raise_next(match="/token")
    creds = _static(http_client, clock)
    with pytest.raises(ConnectivityError, match=r"token endpoint \(ConnectError\)"):
        await creds.refresh()
    assert await creds.bearer_token() == ACCESS_TOKEN


async def test_error_messages_never_contain_secrets(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle, clock: FakeClock
) -> None:
    fake_google.fail_next(401, {"error": "invalid_client"}, match="/token")
    creds = _static(http_client, clock)
    with pytest.raises(AuthenticationError, match="invalid_client") as excinfo:
        await creds.refresh()
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET):
        assert secret not in str(excinfo.value)
