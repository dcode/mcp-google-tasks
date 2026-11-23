"""Where the Google bearer token for an API call comes from.

The API client depends only on the ``CredentialSource`` protocol:

- ``RequestCredentials`` (``oauth-proxy`` and ``resource-server`` modes) reads
  the token FastMCP attached to the current MCP request, so concurrent users
  stay isolated. It cannot refresh; the client re-authenticates instead.
- ``StaticCredentials`` (``token`` mode) holds one user's tokens from the
  environment. A supplied access token is used until Google answers 401; then,
  if refresh credentials exist, it is refreshed once and the call retried.
"""

import asyncio
import math
import time
from collections.abc import Callable
from typing import Protocol

import httpx
import structlog
from fastmcp.server.dependencies import get_access_token
from pydantic import SecretStr

from mcp_google_tasks.errors import AuthenticationError, ConnectivityError

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret

# Treat a refreshed token as expired this many seconds early, so a call never
# starts with a token that lapses in flight.
EXPIRY_SKEW_SECONDS = 60.0
DEFAULT_EXPIRES_IN = 3600.0
REFRESH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

type HttpClientGetter = Callable[[], httpx.AsyncClient]
type Clock = Callable[[], float]

log = structlog.get_logger(__name__)


class CredentialSource(Protocol):
    """Supplies the Google OAuth bearer token for an outgoing API call."""

    async def bearer_token(self) -> str:
        """Return the bearer token to send now.

        Returns:
            A Google OAuth access token.
        """
        ...

    async def refresh(self) -> bool:
        """Try to obtain a new token after Google rejected the current one.

        Returns:
            True if a new token was obtained, so the caller may retry once.
        """
        ...


class RequestCredentials:
    """The token FastMCP verified for the current HTTP request (multi-user modes)."""

    async def bearer_token(self) -> str:
        """Return the current request's Google access token.

        Returns:
            The upstream Google access token.

        Raises:
            AuthenticationError: The request carries no authenticated token.
        """
        token = get_access_token()
        if token is None or not token.token:
            msg = (
                "No authenticated Google OAuth access token found for this request. "
                "Sign in again through your MCP client."
            )
            raise AuthenticationError(msg)
        return token.token

    async def refresh(self) -> bool:
        """Refreshing is the client's job in multi-user modes.

        Returns:
            Always False.
        """
        return False


class StaticCredentials:
    """One user's tokens from the environment (``token`` mode)."""

    def __init__(
        self,
        http: HttpClientGetter,
        *,
        access_token: SecretStr | None,
        refresh_token: SecretStr | None,
        client_id: str | None,
        client_secret: SecretStr | None,
        clock: Clock = time.monotonic,
    ) -> None:
        """Create the credential holder.

        Args:
            http: Returns the shared HTTP client used for the token endpoint.
            access_token: Supplied access token, used until Google rejects it.
            refresh_token: Refresh token; with the client credentials, enables refresh.
            client_id: OAuth client ID that issued the refresh token.
            client_secret: OAuth client secret for that client.
            clock: Monotonic clock in seconds, replaceable in tests.
        """
        self._http = http
        self._token = access_token
        # A supplied token carries no expiry: trust it until Google answers 401.
        self._expires_at = math.inf if access_token is not None else 0.0
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        """Describe the holder without any token value."""
        return f"StaticCredentials(can_refresh={self.can_refresh})"

    @property
    def can_refresh(self) -> bool:
        """Whether a refresh token and the client credentials are all present."""
        return self._refresh_grant() is not None

    def _refresh_grant(self) -> dict[str, str] | None:
        """The token-endpoint form for a refresh, or None if anything is missing."""
        if self._refresh_token is None or self._client_id is None or self._client_secret is None:
            return None
        return {
            "client_id": self._client_id,
            "client_secret": self._client_secret.get_secret_value(),
            "refresh_token": self._refresh_token.get_secret_value(),
            "grant_type": "refresh_token",
        }

    async def bearer_token(self) -> str:
        """Return a token believed valid, refreshing first if it has expired.

        Returns:
            A Google access token.

        Raises:
            AuthenticationError: No token is available and none can be obtained.
            ConnectivityError: The token endpoint could not be reached.
        """
        async with self._lock:
            if self._token is not None and self._clock() < self._expires_at:
                return self._token.get_secret_value()
            grant = self._refresh_grant()
            if grant is None:
                msg = (
                    "The Google access token has expired and no refresh token is configured. "
                    "Set GOOGLE_OAUTH_REFRESH_TOKEN with GOOGLE_OAUTH_CLIENT_ID and "
                    "GOOGLE_OAUTH_CLIENT_SECRET, or supply a fresh GOOGLE_OAUTH_ACCESS_TOKEN."
                )
                raise AuthenticationError(msg)
            return await self._refresh_locked(grant)

    async def refresh(self) -> bool:
        """Refresh after Google rejected the current token.

        Returns:
            True if a new token was obtained; False when no refresh credentials exist.

        Raises:
            AuthenticationError: Google rejected the refresh token.
            ConnectivityError: The token endpoint could not be reached.
        """
        grant = self._refresh_grant()
        if grant is None:
            return False
        async with self._lock:
            await self._refresh_locked(grant)
        return True

    async def _refresh_locked(self, grant: dict[str, str]) -> str:
        """Exchange the refresh token for a new access token; caller holds the lock."""
        try:
            response = await self._http().post(
                GOOGLE_TOKEN_ENDPOINT, data=grant, timeout=REFRESH_TIMEOUT
            )
        except httpx.HTTPError as exc:
            msg = (
                f"Could not reach Google's token endpoint ({exc.__class__.__name__}). "
                "Check network connectivity and try again."
            )
            raise ConnectivityError(msg) from exc

        payload = _json_object(response)
        if response.is_error:
            error = payload.get("error")
            description = payload.get("error_description")
            reason = f"{error}: {description}" if error and description else error or "unknown"
            msg = (
                f"Google refused to refresh the access token (HTTP {response.status_code}, "
                f"{reason}). The refresh token may be revoked or expired; issue a new one."
            )
            raise AuthenticationError(msg)

        new_token = payload.get("access_token")
        if not isinstance(new_token, str) or not new_token:
            msg = "Google's token endpoint answered without an access_token."
            raise AuthenticationError(msg)
        expires_in = payload.get("expires_in")
        ttl = (
            float(expires_in)
            if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
            else DEFAULT_EXPIRES_IN
        )
        self._token = SecretStr(new_token)
        self._expires_at = self._clock() + max(ttl - EXPIRY_SKEW_SECONDS, 0.0)
        log.info("google_token_refreshed", expires_in=ttl)
        return new_token


def _json_object(response: httpx.Response) -> dict[str, object]:
    """Decode a JSON object body, or return an empty dict for anything else."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
