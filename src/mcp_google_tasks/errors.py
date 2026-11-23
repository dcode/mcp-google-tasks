"""Exception types raised on purpose by this package.

Each error carries a plain-language message that is safe to show to the agent
and the user. Messages never contain secrets (tokens, client secrets, keys).
"""


class GoogleTasksError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigurationError(GoogleTasksError):
    """The configuration (environment variables or flags) is missing or invalid."""


class AuthenticationError(GoogleTasksError):
    """No usable Google credential is available, or Google rejected it."""


class ConnectivityError(GoogleTasksError):
    """A Google endpoint could not be reached (network, DNS, TLS trust, timeout)."""


class InvalidArgumentError(GoogleTasksError, ValueError):
    """A tool argument is malformed (bad identifier, bad timestamp, empty patch)."""


class ApiError(GoogleTasksError):
    """A Google API answered with a non-success HTTP status.

    Attributes:
        status_code: HTTP status code of the response.
        api_status: Google's canonical error status (for example ``NOT_FOUND``), if given.
    """

    def __init__(self, status_code: int, message: str, api_status: str | None = None) -> None:
        """Create the error.

        Args:
            status_code: HTTP status code of the response.
            message: Plain-language description from Google or of what went wrong.
            api_status: Google's canonical error status, if the response carried one.
        """
        self.status_code = status_code
        self.api_status = api_status
        detail = f" ({api_status})" if api_status else ""
        super().__init__(f"Google Tasks API error {status_code}{detail}: {message}")
