"""Shared fixtures: an in-process fake of Google served through ``httpx.MockTransport``.

``FakeGoogle`` keeps task lists and tasks in memory and answers every Tasks v1
endpoint the client uses, plus the OAuth token endpoint and OpenID userinfo.
It records each request and has knobs for injecting failures, so no test ever
touches the network.
"""

import itertools
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote

import httpx
import pytest
import structlog
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import FastMCPTransport

from mcp_google_tasks.config import AuthMode, Settings, load_settings
from mcp_google_tasks.logs import configure_logging
from mcp_google_tasks.server import build_server

type JsonObject = dict[str, Any]
type ExceptionFactory = Callable[[httpx.Request], Exception]

TASKS_HOST = "tasks.googleapis.com"
TOKEN_HOST = "oauth2.googleapis.com"  # noqa: S105 - a hostname, not a secret
USERINFO_HOST = "www.googleapis.com"
TASKS_PREFIX = "/tasks/v1"

ACCESS_TOKEN = "test-access-token"  # noqa: S105 - test placeholder
REFRESH_TOKEN = "test-refresh-token"  # noqa: S105 - test placeholder
CLIENT_ID = "test-client-id.apps.example.com"
CLIENT_SECRET = "test-client-secret"  # noqa: S105 - test placeholder
MCP_AUTH_TOKEN = "test-mcp-shared-secret"  # noqa: S105 - test placeholder
BASE_URL = "https://mcp.example.com"
DEFAULT_LIST_ID = "default-list"
TIMESTAMP = "2026-01-01T00:00:00.000Z"

# Minimal valid environment per auth mode, fed through the real ``load_settings``.
TOKEN_ENV: Mapping[str, str] = {
    "MCP_TRANSPORT": "stdio",
    "MCP_AUTH_MODE": "token",
    "GOOGLE_OAUTH_ACCESS_TOKEN": ACCESS_TOKEN,
    "GOOGLE_OAUTH_REFRESH_TOKEN": REFRESH_TOKEN,
    "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
    "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
}
OAUTH_PROXY_ENV: Mapping[str, str] = {
    "MCP_TRANSPORT": "http",
    "MCP_AUTH_MODE": "oauth-proxy",
    "MCP_BASE_URL": BASE_URL,
    "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
    "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
}
RESOURCE_SERVER_ENV: Mapping[str, str] = {
    "MCP_TRANSPORT": "http",
    "MCP_AUTH_MODE": "resource-server",
    "MCP_BASE_URL": BASE_URL,
    "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
}
MODE_ENV: Mapping[AuthMode, Mapping[str, str]] = {
    "token": TOKEN_ENV,
    "oauth-proxy": OAUTH_PROXY_ENV,
    "resource-server": RESOURCE_SERVER_ENV,
}


def google_error(status: int, message: str, api_status: str = "FAILED_PRECONDITION") -> JsonObject:
    """Build an error body in Google's JSON shape."""
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "global", "reason": "failed"}],
            "status": api_status,
        }
    }


def _not_found() -> httpx.Response:
    return httpx.Response(
        404, json=google_error(404, "Requested entity was not found.", "NOT_FOUND")
    )


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request the fake received, decoded for assertions.

    Attributes:
        method: HTTP method.
        host: Target host, e.g. ``tasks.googleapis.com``.
        path: Raw (still percent-encoded) path without the query string.
        params: Query parameters.
        json: Decoded JSON body, or None.
        form: Decoded form body (token endpoint), empty otherwise.
        authorization: The ``Authorization`` header, or None.
    """

    method: str
    host: str
    path: str
    params: dict[str, str]
    json: Any
    form: dict[str, str]
    authorization: str | None


@dataclass(slots=True)
class _Failure:
    """A queued failure: fires on the next matching request(s)."""

    response: httpx.Response | None
    exception: ExceptionFactory | None
    method: str | None
    match: str | None
    times: int

    def matches(self, request: httpx.Request) -> bool:
        if self.method is not None and request.method != self.method:
            return False
        return self.match is None or self.match in unquote(request.url.raw_path.decode())


@dataclass
class FakeGoogle:
    """In-memory Google Tasks, OAuth token endpoint and userinfo.

    State is plain dicts in Google's JSON (camelCase) shape. A fresh fake holds
    one empty list, ``DEFAULT_LIST_ID`` ("My Tasks"), which ``@default``
    resolves to (the first list). Only bearer tokens in ``valid_tokens`` are
    accepted by the Tasks and userinfo endpoints; others get Google's 401.
    """

    tasklists: dict[str, JsonObject] = field(default_factory=dict)
    tasks: dict[str, list[JsonObject]] = field(default_factory=dict)
    valid_tokens: set[str] = field(default_factory=lambda: {ACCESS_TOKEN})
    refresh_token: str = REFRESH_TOKEN
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    refresh_expires_in: int | None = 3600
    userinfo: JsonObject = field(
        default_factory=lambda: {
            "sub": "1234567890",
            "email": "user@example.com",
            "name": "Test User",
        }
    )
    requests: list[RecordedRequest] = field(default_factory=list)
    _failures: list[_Failure] = field(default_factory=list)
    # Quoted: itertools.count is subscriptable at runtime only from Python 3.14, and dataclass
    # annotations are evaluated eagerly before that.
    _ids: "itertools.count[int]" = field(default_factory=lambda: itertools.count(1))
    _refreshes: int = 0

    def __post_init__(self) -> None:
        if not self.tasklists:
            self.add_tasklist("My Tasks", tasklist_id=DEFAULT_LIST_ID)

    # --- Seeding -----------------------------------------------------------

    def add_tasklist(self, title: str, *, tasklist_id: str | None = None) -> JsonObject:
        """Create a task list directly in state and return it."""
        list_id = tasklist_id or f"list-{next(self._ids)}"
        tasklist: JsonObject = {
            "kind": "tasks#taskList",
            "id": list_id,
            "etag": f'"etag-{list_id}"',
            "title": title,
            "updated": TIMESTAMP,
            "selfLink": f"https://{TASKS_HOST}{TASKS_PREFIX}/users/@me/lists/{list_id}",
        }
        self.tasklists[list_id] = tasklist
        self.tasks.setdefault(list_id, [])
        return tasklist

    def add_task(
        self,
        tasklist_id: str,
        title: str,
        *,
        task_id: str | None = None,
        parent: str | None = None,
        **fields: Any,
    ) -> JsonObject:
        """Append a task directly to a list and return it.

        Extra keyword arguments are stored verbatim, so use Google's camelCase
        names (``status="completed"``, ``hidden=True``, ``due=...``).
        """
        list_id = self._resolve(tasklist_id)
        if list_id is None:
            msg = f"no task list {tasklist_id!r} in the fake"
            raise KeyError(msg)
        task = self._new_task(list_id, {"title": title, **fields}, task_id=task_id)
        if parent is not None:
            task["parent"] = parent
        self.tasks[list_id].append(task)
        self._renumber(list_id)
        return task

    def task(self, tasklist_id: str, task_id: str) -> JsonObject | None:
        """Return the stored task, or None."""
        list_id = self._resolve(tasklist_id)
        if list_id is None:
            return None
        return next((t for t in self.tasks[list_id] if t["id"] == task_id), None)

    # --- Failure knobs -----------------------------------------------------

    def fail_next(
        self,
        status: int,
        body: JsonObject | str | None = None,
        *,
        method: str | None = None,
        match: str | None = None,
        times: int = 1,
    ) -> None:
        """Answer the next matching request(s) with an error.

        Args:
            status: HTTP status to return.
            body: A dict is sent as JSON, a str as a non-JSON ``text/plain``
                body; None sends ``google_error(status, "Injected failure")``.
            method: Only match this HTTP method.
            match: Only match requests whose decoded path contains this text
                (e.g. ``"/token"``, ``"/userinfo"``, ``"/lists/abc/tasks"``).
            times: How many matching requests fail before normal service resumes.
        """
        if body is None:
            body = google_error(status, "Injected failure")
        response = (
            httpx.Response(status, text=body)
            if isinstance(body, str)
            else httpx.Response(status, json=body)
        )
        self._failures.append(_Failure(response, None, method, match, times))

    def raise_next(
        self,
        exception: ExceptionFactory | None = None,
        *,
        method: str | None = None,
        match: str | None = None,
        times: int = 1,
    ) -> None:
        """Raise a transport exception on the next matching request(s).

        Args:
            exception: Builds the exception from the request; defaults to
                ``httpx.ConnectError("Connection refused")``.
            method: Only match this HTTP method.
            match: Only match requests whose decoded path contains this text.
            times: How many matching requests raise.
        """
        factory = exception or (lambda r: httpx.ConnectError("Connection refused", request=r))
        self._failures.append(_Failure(None, factory, method, match, times))

    def expire_access_tokens(self) -> None:
        """Invalidate every issued token: the next API call gets 401.

        With refresh credentials configured, the client then refreshes at the
        token endpoint and the retry succeeds (401-once-then-ok).
        """
        self.valid_tokens.clear()

    # --- Request views -----------------------------------------------------

    @property
    def api_requests(self) -> list[RecordedRequest]:
        """Requests to the Tasks API only."""
        return [r for r in self.requests if r.host == TASKS_HOST]

    @property
    def token_requests(self) -> list[RecordedRequest]:
        """Requests to the OAuth token endpoint only."""
        return [r for r in self.requests if r.host == TOKEN_HOST]

    @property
    def last(self) -> RecordedRequest:
        """The most recent request."""
        return self.requests[-1]

    @property
    def transport(self) -> httpx.MockTransport:
        """A transport that routes every request to this fake."""
        return httpx.MockTransport(self.handle)

    # --- Dispatch ----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Record the request, apply queued failures, then route it."""
        self.requests.append(_record(request))
        for failure in self._failures:
            if failure.times > 0 and failure.matches(request):
                failure.times -= 1
                if failure.exception is not None:
                    raise failure.exception(request)
                assert failure.response is not None
                return failure.response
        host = request.url.host
        if host == TOKEN_HOST and request.url.path == "/token":
            return self._token(request)
        if host == USERINFO_HOST and request.url.path == "/oauth2/v3/userinfo":
            return self._authorized(request) or httpx.Response(200, json=self.userinfo)
        if host == TASKS_HOST and request.url.path.startswith(TASKS_PREFIX):
            return self._authorized(request) or self._tasks_api(request)
        return _not_found()

    def _authorized(self, request: httpx.Request) -> httpx.Response | None:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ")
        if header.startswith("Bearer ") and token in self.valid_tokens:
            return None
        body = google_error(
            401, "Request had invalid authentication credentials.", "UNAUTHENTICATED"
        )
        return httpx.Response(401, json=body)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        if form.get("grant_type") != "refresh_token":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if form.get("client_id") != self.client_id or form.get("client_secret") != (
            self.client_secret
        ):
            return httpx.Response(
                401, json={"error": "invalid_client", "error_description": "Unauthorized"}
            )
        if form.get("refresh_token") != self.refresh_token:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Token has been revoked."},
            )
        self._refreshes += 1
        token = f"refreshed-token-{self._refreshes}"
        self.valid_tokens.add(token)
        payload: JsonObject = {"access_token": token, "token_type": "Bearer"}
        if self.refresh_expires_in is not None:
            payload["expires_in"] = self.refresh_expires_in
        return httpx.Response(200, json=payload)

    def _tasks_api(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?", 1)[0]
        parts = [unquote(p) for p in raw.removeprefix(TASKS_PREFIX).strip("/").split("/")]
        params = dict(request.url.params)
        body = _json_body(request)
        match request.method, parts:
            case "GET", ["users", "@me", "lists"]:
                return self._page(list(self.tasklists.values()), params, "tasks#taskLists")
            case "POST", ["users", "@me", "lists"]:
                return _ok(self.add_tasklist(str(body.get("title", ""))))
            case method, ["users", "@me", "lists", list_id]:
                return self._tasklist(method, list_id, body)
            case "GET", ["lists", list_id, "tasks"]:
                return self._list_tasks(list_id, params)
            case "POST", ["lists", list_id, "tasks"]:
                return self._insert_task(list_id, body, params)
            case "POST", ["lists", list_id, "tasks", task_id, "move"]:
                return self._move_task(list_id, task_id, params)
            case method, ["lists", list_id, "tasks", task_id]:
                return self._task(method, list_id, task_id, body)
            case "POST", ["lists", list_id, "clear"]:
                return self._clear(list_id)
            case _:
                return _not_found()

    # --- Task lists --------------------------------------------------------

    def _resolve(self, tasklist_id: str) -> str | None:
        if tasklist_id == "@default":
            return next(iter(self.tasklists), None)
        return tasklist_id if tasklist_id in self.tasklists else None

    def _tasklist(self, method: str, tasklist_id: str, body: JsonObject) -> httpx.Response:
        list_id = self._resolve(tasklist_id)
        if list_id is None:
            return _not_found()
        tasklist = self.tasklists[list_id]
        match method:
            case "GET":
                return _ok(tasklist)
            case "PUT" | "PATCH":
                if "title" in body:
                    tasklist["title"] = body["title"]
                return _ok(tasklist)
            case "DELETE":
                del self.tasklists[list_id]
                del self.tasks[list_id]
                return httpx.Response(204)
            case _:
                return _not_found()

    # --- Tasks -------------------------------------------------------------

    def _new_task(self, list_id: str, body: JsonObject, *, task_id: str | None) -> JsonObject:
        new_id = task_id or f"task-{next(self._ids)}"
        task: JsonObject = {
            "kind": "tasks#task",
            "id": new_id,
            "etag": f'"etag-{new_id}"',
            "title": "",
            "updated": TIMESTAMP,
            "selfLink": f"https://{TASKS_HOST}{TASKS_PREFIX}/lists/{list_id}/tasks/{new_id}",
            "status": "needsAction",
            "links": [],
            "webViewLink": f"https://tasks.example.com/task/{new_id}",
        }
        task.update({k: v for k, v in body.items() if v is not None and k != "id"})
        return task

    def _renumber(self, list_id: str) -> None:
        for index, task in enumerate(self.tasks[list_id]):
            task["position"] = f"{index:020d}"

    def _place(self, list_id: str, task: JsonObject, previous: str | None) -> None:
        tasks = self.tasks[list_id]
        index = next((i + 1 for i, t in enumerate(tasks) if t["id"] == previous), 0)
        tasks.insert(index, task)
        self._renumber(list_id)

    def _list_tasks(self, tasklist_id: str, params: dict[str, str]) -> httpx.Response:
        list_id = self._resolve(tasklist_id)
        if list_id is None:
            return _not_found()
        show_completed = params.get("showCompleted", "true") == "true"
        show_hidden = params.get("showHidden", "false") == "true"
        show_deleted = params.get("showDeleted", "false") == "true"
        visible = [
            t
            for t in self.tasks[list_id]
            if (show_completed or t.get("status") != "completed")
            and (show_hidden or not t.get("hidden"))
            and (show_deleted or not t.get("deleted"))
        ]
        return self._page(visible, params, "tasks#tasks")

    def _insert_task(
        self, tasklist_id: str, body: JsonObject, params: dict[str, str]
    ) -> httpx.Response:
        list_id = self._resolve(tasklist_id)
        if list_id is None:
            return _not_found()
        task = self._new_task(list_id, body, task_id=None)
        if "parent" in params:
            task["parent"] = params["parent"]
        self._place(list_id, task, params.get("previous"))
        return _ok(task)

    def _task(
        self, method: str, tasklist_id: str, task_id: str, body: JsonObject
    ) -> httpx.Response:
        list_id = self._resolve(tasklist_id)
        task = None if list_id is None else self.task(list_id, task_id)
        if list_id is None or task is None:
            return _not_found()
        match method:
            case "GET":
                return _ok(task)
            case "PUT":
                kept = {k: task[k] for k in ("kind", "id", "etag", "selfLink", "position")}
                task.clear()
                task.update(kept)
                task.update({k: v for k, v in body.items() if v is not None and k != "id"})
                return _ok(task)
            case "PATCH":
                for key, value in body.items():
                    if value is None:
                        task.pop(key, None)
                    elif key != "id":
                        task[key] = value
                return _ok(task)
            case "DELETE":
                self.tasks[list_id].remove(task)
                self._renumber(list_id)
                return httpx.Response(204)
            case _:
                return _not_found()

    def _move_task(self, tasklist_id: str, task_id: str, params: dict[str, str]) -> httpx.Response:
        list_id = self._resolve(tasklist_id)
        task = None if list_id is None else self.task(list_id, task_id)
        if list_id is None or task is None:
            return _not_found()
        destination = self._resolve(params.get("destinationTasklist", list_id))
        if destination is None:
            return _not_found()
        self.tasks[list_id].remove(task)
        self._renumber(list_id)
        task.pop("parent", None)
        if "parent" in params:
            task["parent"] = params["parent"]
        self._place(destination, task, params.get("previous"))
        return _ok(task)

    def _clear(self, tasklist_id: str) -> httpx.Response:
        list_id = self._resolve(tasklist_id)
        if list_id is None:
            return _not_found()
        for task in self.tasks[list_id]:
            if task.get("status") == "completed":
                task["hidden"] = True
        return httpx.Response(204)

    @staticmethod
    def _page(items: list[JsonObject], params: dict[str, str], kind: str) -> httpx.Response:
        try:
            size = int(params.get("maxResults", "20"))
            start = int(params.get("pageToken", "0"))
        except ValueError:
            return httpx.Response(
                400, json=google_error(400, "Invalid page token.", "INVALID_ARGUMENT")
            )
        page: JsonObject = {
            "kind": kind,
            "etag": '"etag-page"',
            "items": items[start : start + size],
        }
        if start + size < len(items):
            page["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=page)


def _ok(payload: JsonObject) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _json_body(request: httpx.Request) -> JsonObject:
    content_type = request.headers.get("content-type", "")
    if not request.content or "json" not in content_type:
        return {}
    data = json.loads(request.content)
    return data if isinstance(data, dict) else {}


def _record(request: httpx.Request) -> RecordedRequest:
    content_type = request.headers.get("content-type", "")
    body: Any = None
    if request.content and "json" in content_type:
        body = json.loads(request.content)
    form = (
        dict(parse_qsl(request.content.decode())) if "x-www-form-urlencoded" in content_type else {}
    )
    return RecordedRequest(
        method=request.method,
        host=request.url.host,
        path=request.url.raw_path.decode().split("?", 1)[0],
        params=dict(request.url.params),
        json=body,
        form=form,
        authorization=request.headers.get("authorization"),
    )


# --- Fixtures ----------------------------------------------------------------


class SettingsFactory(Protocol):
    """Builds ``Settings`` through the real ``load_settings``."""

    def __call__(self, auth_mode: AuthMode = "token", /, **env: str | None) -> Settings:
        """Return settings for ``auth_mode``; ``env`` overrides keys (None deletes one)."""
        ...


class ServerFactory(Protocol):
    """Builds a server wired to the fake Google over the shared ``http_client``."""

    def __call__(self, settings: Settings | None = None, /) -> FastMCP:
        """Return ``build_server(settings, http_client=...)``; token mode when omitted."""
        ...


@pytest.fixture(autouse=True)
def _logging_to_stderr() -> None:
    """Route structlog to stderr at debug so stdout stays clean and every log call runs."""
    configure_logging("debug")
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def fake_google() -> FakeGoogle:
    """A fresh fake Google with one empty default list."""
    return FakeGoogle()


@pytest.fixture
async def http_client(fake_google: FakeGoogle) -> AsyncIterator[httpx.AsyncClient]:
    """An ``httpx.AsyncClient`` whose every request goes to ``fake_google``.

    No ``base_url``: the source sends absolute Google URLs.
    """
    async with httpx.AsyncClient(transport=fake_google.transport) as client:
        yield client


@pytest.fixture
def make_settings() -> SettingsFactory:
    """Factory for ``Settings`` per auth mode, built from ``MODE_ENV`` plus overrides."""

    def factory(auth_mode: AuthMode = "token", /, **env: str | None) -> Settings:
        merged: dict[str, str | None] = {**MODE_ENV[auth_mode], **env}
        return load_settings({k: v for k, v in merged.items() if v is not None})

    return factory


@pytest.fixture
def token_settings(make_settings: SettingsFactory) -> Settings:
    """Token-mode settings over stdio with access and refresh credentials."""
    return make_settings("token")


@pytest.fixture
def make_server(http_client: httpx.AsyncClient, token_settings: Settings) -> ServerFactory:
    """Factory for servers that share ``http_client`` (and so ``fake_google``)."""

    def factory(settings: Settings | None = None, /) -> FastMCP:
        return build_server(settings or token_settings, http_client=http_client)

    return factory


@pytest.fixture
async def mcp_client(make_server: ServerFactory) -> AsyncIterator[Client[FastMCPTransport]]:
    """A connected in-memory MCP client for a token-mode server."""
    async with Client(make_server()) as client:
        yield client


# --- Helpers for tests -------------------------------------------------------


async def call_tool(
    client: Client[FastMCPTransport], name: str, arguments: Mapping[str, Any] | None = None
) -> Any:
    """Call a tool that must succeed and return its structured content."""
    result = await client.call_tool(name, dict(arguments or {}))
    assert not result.is_error, result.content
    return result.structured_content


async def tool_error(
    client: Client[FastMCPTransport], name: str, arguments: Mapping[str, Any] | None = None
) -> str:
    """Call a tool that must fail and return the error text the model sees."""
    result: CallToolResult = await client.call_tool(
        name, dict(arguments or {}), raise_on_error=False
    )
    assert result.is_error, result.structured_content
    return " ".join(getattr(block, "text", "") for block in result.content)
