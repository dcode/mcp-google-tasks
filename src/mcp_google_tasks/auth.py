"""Authentication providers for the three auth modes.

- ``oauth-proxy``: FastMCP's ``GoogleProvider`` runs the OAuth flow itself
  (dynamic client registration / CIMD) and keeps upstream tokens in storage.
- ``resource-server``: a gateway such as Obot runs OAuth and forwards
  ``Authorization: Bearer <google token>``. Tokens are verified with Google's
  tokeninfo endpoint and must be minted for one of the configured OAuth
  client IDs (audience), which stops a token issued to another app from being
  replayed here. RFC 9728 protected-resource metadata is served.
- ``token``: single-user; Google tokens come from the environment. Over HTTP,
  an optional shared secret (``MCP_AUTH_TOKEN``) gates the endpoint.
"""

import hmac
from typing import assert_never, override

from fastmcp.server.auth import AuthProvider, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider, GoogleTokenVerifier
from pydantic import AnyHttpUrl, SecretStr

from mcp_google_tasks.config import Settings
from mcp_google_tasks.errors import ConfigurationError
from mcp_google_tasks.storage import build_client_storage

GOOGLE_AUTHORIZATION_SERVER = "https://accounts.google.com"
RESOURCE_NAME = "Google Tasks MCP Server"
SINGLE_USER_CLIENT_ID = "single-user"


class SharedSecretVerifier(TokenVerifier):
    """Accepts exactly one bearer value, compared in constant time."""

    def __init__(self, secret: SecretStr) -> None:
        """Create the verifier.

        Args:
            secret: The bearer value clients must send.
        """
        super().__init__()
        self._secret = secret.get_secret_value().encode("utf-8")

    @override
    def __repr__(self) -> str:
        return "SharedSecretVerifier()"

    @override
    async def verify_token(self, token: str) -> AccessToken | None:
        """Accept the request only if ``token`` equals the configured secret.

        Args:
            token: Bearer value from the ``Authorization`` header.

        Returns:
            An access token with no scopes on a match, otherwise None.
        """
        if hmac.compare_digest(token.encode("utf-8"), self._secret):
            return AccessToken(token=token, client_id=SINGLE_USER_CLIENT_ID, scopes=[])
        return None


class TasksScopeVerifier(TokenVerifier):
    """Wraps a Google verifier and requires any one of the accepted Tasks scopes.

    ``GoogleTokenVerifier`` can only require *all* of a scope list. In
    read-only mode either the full or the read-only Tasks scope is enough, so
    the check happens here instead.
    """

    def __init__(self, inner: TokenVerifier, accepted_scopes: frozenset[str]) -> None:
        """Create the wrapper.

        Args:
            inner: Verifier that checks the token itself (signature, audience).
            accepted_scopes: The token must carry at least one of these.
        """
        super().__init__()
        self._inner = inner
        self._accepted = accepted_scopes

    @override
    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify with the inner verifier, then check the Tasks scope.

        Args:
            token: Bearer value from the ``Authorization`` header.

        Returns:
            The verified token, or None if it is invalid or lacks a Tasks scope.
        """
        verified = await self._inner.verify_token(token)
        if verified is None or self._accepted.isdisjoint(verified.scopes):
            return None
        return verified


def build_auth_provider(settings: Settings) -> AuthProvider | None:
    """Build the auth provider for the configured mode.

    Args:
        settings: Validated settings.

    Returns:
        The provider, or None when no HTTP authentication applies (``token``
        mode over stdio, or over HTTP without ``MCP_AUTH_TOKEN``).

    Raises:
        ConfigurationError: A setting the mode needs is missing.
    """
    match settings.auth_mode:
        case "oauth-proxy":
            return _oauth_proxy(settings)
        case "resource-server":
            return _resource_server(settings)
        case "token":
            if settings.transport == "http" and settings.mcp_auth_token is not None:
                return SharedSecretVerifier(settings.mcp_auth_token)
            return None
        case _:
            # Fail closed: an unknown mode must never mean "no authentication".
            assert_never(settings.auth_mode)


def _oauth_proxy(settings: Settings) -> GoogleProvider:
    """FastMCP's Google OAuth proxy with optional persistent storage."""
    if settings.client_id is None or settings.client_secret is None:
        msg = "oauth-proxy mode needs GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
        raise ConfigurationError(msg)
    jwt_key = settings.jwt_signing_key
    return GoogleProvider(
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        base_url=settings.base_url,
        redirect_path=settings.redirect_path,
        required_scopes=settings.required_scopes,
        allowed_client_redirect_uris=(
            list(settings.allowed_client_redirect_uris)
            if settings.allowed_client_redirect_uris is not None
            else None
        ),
        jwt_signing_key=jwt_key.get_secret_value() if jwt_key is not None else None,
        client_storage=build_client_storage(settings),
    )


def _resource_server(settings: Settings) -> RemoteAuthProvider:
    """Verify forwarded Google tokens and serve RFC 9728 metadata."""
    if not settings.audiences:
        msg = "resource-server mode needs GOOGLE_OAUTH_CLIENT_ID or GOOGLE_OAUTH_AUDIENCE."
        raise ConfigurationError(msg)
    audience = list(settings.audiences)
    google = GoogleTokenVerifier(
        audience=audience[0] if len(audience) == 1 else audience,
        cache_ttl_seconds=settings.token_cache_ttl_seconds or None,
    )
    return RemoteAuthProvider(
        token_verifier=TasksScopeVerifier(google, settings.accepted_tasks_scopes),
        authorization_servers=[AnyHttpUrl(GOOGLE_AUTHORIZATION_SERVER)],
        base_url=settings.base_url,
        scopes_supported=[settings.tasks_scope],
        challenge_scopes=[settings.tasks_scope],
        resource_name=RESOURCE_NAME,
    )
