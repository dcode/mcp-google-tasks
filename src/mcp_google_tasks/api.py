"""The seam between the MCP tools and the Google Tasks REST API v1.

Tools depend only on the ``TasksApi`` protocol. ``GoogleTasksClient`` is the
real adapter: it takes a shared ``httpx.AsyncClient`` and a
``CredentialSource`` and knows nothing about transports or auth modes, so
tests drive it with ``httpx.MockTransport`` and a fake credential source.
"""

import unicodedata
from collections.abc import Mapping
from typing import Literal, Protocol
from urllib.parse import quote

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from mcp_google_tasks.credentials import CredentialSource
from mcp_google_tasks.errors import ApiError, ConnectivityError, InvalidArgumentError
from mcp_google_tasks.models import (
    CurrentUserInfo,
    Task,
    TaskList,
    TaskListsResponse,
    TasksResponse,
)

GOOGLE_TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"
GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Resources read every page; these bound the work one read can cause.
PAGE_SIZE = 100
MAX_PAGES = 50

# Identifiers Google defines as aliases, passed through unencoded.
_ALIASES = frozenset({"@default", "@me"})

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type QueryParamValue = str | int | bool
type QueryParams = Mapping[str, QueryParamValue]
type RequestBody = dict[str, str | bool | None]

log = structlog.get_logger(__name__)


class TasksApi(Protocol):
    """One async method per Google Tasks endpoint the tools use."""

    async def list_tasklists(
        self, *, max_results: int | None = None, page_token: str | None = None
    ) -> TaskListsResponse:
        """List the user's task lists (tasklists.list)."""
        ...

    async def get_tasklist(self, tasklist_id: str) -> TaskList:
        """Get one task list (tasklists.get)."""
        ...

    async def insert_tasklist(self, title: str) -> TaskList:
        """Create a task list (tasklists.insert)."""
        ...

    async def update_tasklist(self, tasklist_id: str, title: str) -> TaskList:
        """Replace a task list (tasklists.update)."""
        ...

    async def patch_tasklist(self, tasklist_id: str, title: str) -> TaskList:
        """Partially update a task list (tasklists.patch)."""
        ...

    async def delete_tasklist(self, tasklist_id: str) -> None:
        """Delete a task list (tasklists.delete)."""
        ...

    async def list_tasks(
        self, tasklist_id: str, *, query: QueryParams | None = None
    ) -> TasksResponse:
        """List tasks in a list (tasks.list)."""
        ...

    async def get_task(self, tasklist_id: str, task_id: str) -> Task:
        """Get one task (tasks.get)."""
        ...

    async def insert_task(
        self,
        tasklist_id: str,
        body: RequestBody,
        *,
        parent: str | None = None,
        previous: str | None = None,
    ) -> Task:
        """Create a task (tasks.insert)."""
        ...

    async def update_task(self, tasklist_id: str, task_id: str, body: RequestBody) -> Task:
        """Replace a task (tasks.update)."""
        ...

    async def patch_task(self, tasklist_id: str, task_id: str, body: RequestBody) -> Task:
        """Partially update a task (tasks.patch)."""
        ...

    async def delete_task(self, tasklist_id: str, task_id: str) -> None:
        """Delete a task (tasks.delete)."""
        ...

    async def move_task(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        parent: str | None = None,
        previous: str | None = None,
        destination_tasklist: str | None = None,
    ) -> Task:
        """Move or reparent a task (tasks.move)."""
        ...

    async def clear_tasks(self, tasklist_id: str) -> None:
        """Hide all completed tasks in a list (tasks.clear)."""
        ...

    async def get_userinfo(self) -> CurrentUserInfo:
        """Get the token owner's profile from the OpenID userinfo endpoint."""
        ...


def encode_segment(value: str, what: str = "identifier") -> str:
    """Validate and percent-encode one URL path segment.

    Args:
        value: The identifier to place in the path.
        what: Name of the argument, for the error message.

    Returns:
        The encoded segment. ``@default`` and ``@me`` pass through unchanged;
        everything else is encoded with no safe characters, so ``/``, ``?``
        and ``#`` cannot change the request path.

    Raises:
        InvalidArgumentError: The value is empty, ``.``/``..``, or holds control characters.
    """
    if value in _ALIASES:
        return quote(value, safe="@")
    if not value or value.strip() == "":
        msg = f"{what} must not be empty."
        raise InvalidArgumentError(msg)
    if value in {".", ".."}:
        msg = f"{what} must not be {value!r}."
        raise InvalidArgumentError(msg)
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        msg = f"{what} must not contain control characters."
        raise InvalidArgumentError(msg)
    return quote(value, safe="")


def _clean_params(params: QueryParams | None) -> dict[str, str] | None:
    """Render query values as Google expects (booleans as ``true``/``false``)."""
    if not params:
        return None
    return {
        key: ("true" if value else "false") if isinstance(value, bool) else str(value)
        for key, value in params.items()
    }


def extract_google_error(response: httpx.Response) -> tuple[str, str | None]:
    """Extract a readable message and status string from a Google API error.

    Args:
        response: The error response.

    Returns:
        ``(message, api_status)``; ``api_status`` is None when Google gave none.
    """
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return (text or response.reason_phrase or "Unknown error", None)

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or response.reason_phrase)
            status = err.get("status")
            return (message, str(status) if status is not None else None)
        if isinstance(err, str):
            return (err, None)
    return (response.text.strip() or response.reason_phrase, None)


def _parse[M: BaseModel](model: type[M], response: httpx.Response) -> M:
    """Validate a JSON body into ``model``, mapping surprises to ``ApiError``."""
    try:
        return model.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        msg = f"Unexpected response body from Google ({model.__name__} expected)."
        raise ApiError(response.status_code, msg) from exc


class GoogleTasksClient:
    """``TasksApi`` over HTTPS with httpx."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        credentials: CredentialSource,
        *,
        base_url: str = GOOGLE_TASKS_API_BASE,
    ) -> None:
        """Create the adapter.

        Args:
            http: Shared, pooled client; it carries no credentials of its own.
            credentials: Supplies the bearer token for each request.
            base_url: Root of the Tasks API, replaceable in tests.
        """
        self._http = http
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")

    async def _request(
        self,
        method: HttpMethod,
        url: str,
        *,
        params: QueryParams | None = None,
        json_body: RequestBody | None = None,
    ) -> httpx.Response:
        """Send an authenticated request; on 401, refresh once and retry.

        Raises:
            ConnectivityError: Google could not be reached.
            ApiError: Google answered with a non-success status.
        """
        clean = _clean_params(params)
        refreshed = False
        while True:
            token = await self._credentials.bearer_token()
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=clean,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                msg = (
                    f"Could not reach Google ({exc.__class__.__name__}). "
                    "Check network connectivity and try again."
                )
                raise ConnectivityError(msg) from exc
            log.debug("google_response", method=method, status=response.status_code)
            if (
                response.status_code == httpx.codes.UNAUTHORIZED
                and not refreshed
                and await self._credentials.refresh()
            ):
                refreshed = True
                continue
            if response.is_error:
                message, api_status = extract_google_error(response)
                raise ApiError(response.status_code, message, api_status)
            return response

    def _url(self, *segments: str) -> str:
        """Join pre-encoded segments onto the Tasks API root."""
        return "/".join((self._base_url, *segments))

    @staticmethod
    def _list(tasklist_id: str) -> str:
        return encode_segment(tasklist_id, "tasklist_id")

    @staticmethod
    def _task(task_id: str) -> str:
        return encode_segment(task_id, "task_id")

    async def list_tasklists(
        self, *, max_results: int | None = None, page_token: str | None = None
    ) -> TaskListsResponse:
        """List the user's task lists (tasklists.list).

        Args:
            max_results: Page size (1-100).
            page_token: Token from a previous page.

        Returns:
            One page of task lists.
        """
        params: dict[str, QueryParamValue] = {}
        if max_results is not None:
            params["maxResults"] = max_results
        if page_token is not None:
            params["pageToken"] = page_token
        response = await self._request("GET", self._url("users", "@me", "lists"), params=params)
        return _parse(TaskListsResponse, response)

    async def get_tasklist(self, tasklist_id: str) -> TaskList:
        """Get one task list (tasklists.get).

        Args:
            tasklist_id: Task list ID or ``@default``.

        Returns:
            The task list.
        """
        url = self._url("users", "@me", "lists", self._list(tasklist_id))
        return _parse(TaskList, await self._request("GET", url))

    async def insert_tasklist(self, title: str) -> TaskList:
        """Create a task list (tasklists.insert).

        Args:
            title: Title of the new list.

        Returns:
            The created task list.
        """
        url = self._url("users", "@me", "lists")
        return _parse(TaskList, await self._request("POST", url, json_body={"title": title}))

    async def update_tasklist(self, tasklist_id: str, title: str) -> TaskList:
        """Replace a task list (tasklists.update).

        Args:
            tasklist_id: Task list ID or ``@default``.
            title: New title.

        Returns:
            The updated task list.
        """
        url = self._url("users", "@me", "lists", self._list(tasklist_id))
        body: RequestBody = {"id": tasklist_id, "title": title}
        return _parse(TaskList, await self._request("PUT", url, json_body=body))

    async def patch_tasklist(self, tasklist_id: str, title: str) -> TaskList:
        """Partially update a task list (tasklists.patch).

        Args:
            tasklist_id: Task list ID or ``@default``.
            title: New title.

        Returns:
            The updated task list.
        """
        url = self._url("users", "@me", "lists", self._list(tasklist_id))
        return _parse(TaskList, await self._request("PATCH", url, json_body={"title": title}))

    async def delete_tasklist(self, tasklist_id: str) -> None:
        """Delete a task list and all of its tasks (tasklists.delete).

        Args:
            tasklist_id: Task list ID.
        """
        await self._request("DELETE", self._url("users", "@me", "lists", self._list(tasklist_id)))

    async def list_tasks(
        self, tasklist_id: str, *, query: QueryParams | None = None
    ) -> TasksResponse:
        """List tasks in a list (tasks.list).

        Args:
            tasklist_id: Task list ID or ``@default``.
            query: Google query parameters (camelCase names), already validated.

        Returns:
            One page of tasks.
        """
        url = self._url("lists", self._list(tasklist_id), "tasks")
        return _parse(TasksResponse, await self._request("GET", url, params=query))

    async def get_task(self, tasklist_id: str, task_id: str) -> Task:
        """Get one task (tasks.get).

        Args:
            tasklist_id: Task list ID or ``@default``.
            task_id: Task ID.

        Returns:
            The task.
        """
        url = self._url("lists", self._list(tasklist_id), "tasks", self._task(task_id))
        return _parse(Task, await self._request("GET", url))

    async def insert_task(
        self,
        tasklist_id: str,
        body: RequestBody,
        *,
        parent: str | None = None,
        previous: str | None = None,
    ) -> Task:
        """Create a task (tasks.insert).

        Args:
            tasklist_id: Task list ID or ``@default``.
            body: Task fields (camelCase names).
            parent: Parent task ID, to create a subtask.
            previous: Sibling task ID to insert after.

        Returns:
            The created task.
        """
        params: dict[str, QueryParamValue] = {}
        if parent is not None:
            params["parent"] = parent
        if previous is not None:
            params["previous"] = previous
        url = self._url("lists", self._list(tasklist_id), "tasks")
        return _parse(Task, await self._request("POST", url, params=params, json_body=body))

    async def update_task(self, tasklist_id: str, task_id: str, body: RequestBody) -> Task:
        """Replace a task (tasks.update).

        Args:
            tasklist_id: Task list ID or ``@default``.
            task_id: Task ID.
            body: Full task body (camelCase names).

        Returns:
            The updated task.
        """
        url = self._url("lists", self._list(tasklist_id), "tasks", self._task(task_id))
        return _parse(Task, await self._request("PUT", url, json_body=body))

    async def patch_task(self, tasklist_id: str, task_id: str, body: RequestBody) -> Task:
        """Partially update a task (tasks.patch).

        Args:
            tasklist_id: Task list ID or ``@default``.
            task_id: Task ID.
            body: Fields to change (camelCase names); ``None`` clears a field.

        Returns:
            The updated task.
        """
        url = self._url("lists", self._list(tasklist_id), "tasks", self._task(task_id))
        return _parse(Task, await self._request("PATCH", url, json_body=body))

    async def delete_task(self, tasklist_id: str, task_id: str) -> None:
        """Delete a task (tasks.delete).

        Args:
            tasklist_id: Task list ID or ``@default``.
            task_id: Task ID.
        """
        url = self._url("lists", self._list(tasklist_id), "tasks", self._task(task_id))
        await self._request("DELETE", url)

    async def move_task(
        self,
        tasklist_id: str,
        task_id: str,
        *,
        parent: str | None = None,
        previous: str | None = None,
        destination_tasklist: str | None = None,
    ) -> Task:
        """Move or reparent a task (tasks.move).

        Args:
            tasklist_id: Source task list ID or ``@default``.
            task_id: Task ID.
            parent: New parent task ID.
            previous: New previous sibling task ID.
            destination_tasklist: Destination list ID, to move across lists.

        Returns:
            The moved task.
        """
        params: dict[str, QueryParamValue] = {}
        if parent is not None:
            params["parent"] = parent
        if previous is not None:
            params["previous"] = previous
        if destination_tasklist is not None:
            params["destinationTasklist"] = destination_tasklist
        url = self._url("lists", self._list(tasklist_id), "tasks", self._task(task_id), "move")
        return _parse(Task, await self._request("POST", url, params=params))

    async def clear_tasks(self, tasklist_id: str) -> None:
        """Hide all completed tasks in a list (tasks.clear).

        Args:
            tasklist_id: Task list ID or ``@default``.
        """
        await self._request("POST", self._url("lists", self._list(tasklist_id), "clear"))

    async def get_userinfo(self) -> CurrentUserInfo:
        """Get the token owner's profile from the OpenID userinfo endpoint.

        Returns:
            The user's ``sub``, ``email`` and ``name``; ``scopes`` is empty
            because userinfo does not report them.
        """
        response = await self._request("GET", GOOGLE_USERINFO_ENDPOINT)
        try:
            data = response.json()
        except ValueError as exc:
            msg = "Unexpected response body from Google userinfo."
            raise ApiError(response.status_code, msg) from exc
        if not isinstance(data, dict):
            msg = "Unexpected response body from Google userinfo."
            raise ApiError(response.status_code, msg)
        return CurrentUserInfo(
            sub=_opt_str(data.get("sub")),
            email=_opt_str(data.get("email")),
            name=_opt_str(data.get("name")),
            scopes=[],
        )


def _opt_str(value: object) -> str | None:
    """Stringify a claim, keeping None as None."""
    return str(value) if value is not None else None


async def fetch_all_tasklists(api: TasksApi) -> TaskListsResponse:
    """Read every page of task lists, up to ``MAX_PAGES`` pages.

    Args:
        api: The Tasks API.

    Returns:
        All task lists. If the cap was hit, ``next_page_token`` holds the token
        for the remaining pages so the truncation is visible.
    """
    first = await api.list_tasklists(max_results=PAGE_SIZE)
    items = list(first.items)
    token = first.next_page_token
    pages = 1
    while token and pages < MAX_PAGES:
        page = await api.list_tasklists(max_results=PAGE_SIZE, page_token=token)
        items.extend(page.items)
        token = page.next_page_token
        pages += 1
    return first.model_copy(update={"items": items, "next_page_token": token})


async def fetch_all_tasks(api: TasksApi, tasklist_id: str) -> TasksResponse:
    """Read every page of tasks in a list, up to ``MAX_PAGES`` pages.

    Args:
        api: The Tasks API.
        tasklist_id: Task list ID or ``@default``.

    Returns:
        All tasks with Google's default filters. If the cap was hit,
        ``next_page_token`` holds the token for the remaining pages.
    """
    first = await api.list_tasks(tasklist_id, query={"maxResults": PAGE_SIZE})
    items = list(first.items)
    token = first.next_page_token
    pages = 1
    while token and pages < MAX_PAGES:
        page = await api.list_tasks(
            tasklist_id, query={"maxResults": PAGE_SIZE, "pageToken": token}
        )
        items.extend(page.items)
        token = page.next_page_token
        pages += 1
    return first.model_copy(update={"items": items, "next_page_token": token})
