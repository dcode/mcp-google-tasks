"""MCP tools and resources for Google Tasks.

Tools never make HTTP calls themselves: each one asks ``api_factory`` for a
``TasksApi`` and calls it. Errors raised on purpose reach the model with their
message (see ``anticipated``); FastMCP hides the text of anything else.
"""

import functools
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_google_tasks.api import (
    QueryParamValue,
    RequestBody,
    TasksApi,
    fetch_all_tasklists,
    fetch_all_tasks,
)
from mcp_google_tasks.errors import AuthenticationError, GoogleTasksError, InvalidArgumentError
from mcp_google_tasks.models import (
    CurrentUserInfo,
    OperationResult,
    Task,
    TaskList,
    TaskListsResponse,
    TasksResponse,
    TaskStatus,
)

type ApiFactory = Callable[[], TasksApi]
type IdentitySource = Literal["userinfo", "token-claims"]

# Hints let agent hosts decide when to ask the user before a call. Values match
# the original server's annotations exactly.
IDENTITY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, open_world_hint=True)
IDEMPOTENT_WRITE = ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

_TASKLIST_DEFAULT_DESC = "Task list ID, or '@default' for the primary list."


def anticipated[**P, R](tool: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Pass this package's own errors to the model with their message intact.

    FastMCP hides the text of unexpected exceptions from the model. Errors
    raised on purpose carry plain-language guidance, so they are re-raised as
    ``ToolError``, whose message is passed through.

    Args:
        tool: The async tool function to wrap.

    Returns:
        The wrapped tool, with the same signature.
    """

    @functools.wraps(tool)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await tool(*args, **kwargs)
        except (GoogleTasksError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _anticipated_resource[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Like ``anticipated``, for resources (re-raised as ``ResourceError``)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (GoogleTasksError, ValueError) as exc:
            raise ResourceError(str(exc)) from exc

    return wrapper


def check_rfc3339(value: str | None, name: str) -> str | None:
    """Validate an RFC 3339 timestamp argument.

    Args:
        value: The timestamp, or None when the argument was omitted.
        name: Argument name, for the error message.

    Returns:
        The stripped timestamp unchanged, or None.

    Raises:
        InvalidArgumentError: The value is not a timezone-aware RFC 3339 timestamp.
    """
    if value is None:
        return None
    text = value.strip()
    candidate = f"{text[:-1]}+00:00" if text[-1:] in {"Z", "z"} else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        msg = (
            f"{name} must be an RFC 3339 timestamp with a timezone, such as "
            f"'2026-12-01T00:00:00Z'; got {value!r}."
        )
        raise InvalidArgumentError(msg)
    return text


def _claim(claims: dict[str, object], key: str) -> str | None:
    """Stringify one claim, keeping a missing claim as None."""
    value = claims.get(key)
    return str(value) if value is not None else None


def register_tools(
    mcp: FastMCP,
    api_factory: ApiFactory,
    *,
    readonly: bool,
    identity: IdentitySource,
) -> None:
    """Register the Google Tasks tools on a FastMCP server.

    Args:
        mcp: The server to add tools to.
        api_factory: Returns the Tasks API to use for the current call.
        readonly: When True, write tools are not registered at all.
        identity: ``userinfo`` (single-user token mode: ask Google) or
            ``token-claims`` (multi-user: read the verified request token).
    """

    def tool[**P, R](
        annotations: ToolAnnotations,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        def register(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            if readonly and not annotations.read_only_hint:
                return fn
            mcp.tool(annotations=annotations)(anticipated(fn))
            return fn

        return register

    # --- Identity / Diagnostics ---

    @tool(IDENTITY)
    async def get_current_user() -> CurrentUserInfo:
        """Return profile claims and granted scopes for the authenticated user.

        Call this to confirm which Google account the tools act on, or to diagnose
        a permission error (check that a Tasks scope is granted). In single-user
        token mode the profile comes from Google's userinfo endpoint and the scope
        list is empty. Fails if no Google credential is available; the user must
        sign in again or fix the server's token configuration.
        """
        if identity == "userinfo":
            return await api_factory().get_userinfo()
        token = get_access_token()
        if token is None:
            msg = "No authenticated Google OAuth access token found for this request."
            raise AuthenticationError(msg)
        claims: dict[str, object] = dict(token.claims or {})
        return CurrentUserInfo(
            sub=_claim(claims, "sub"),
            email=_claim(claims, "email"),
            name=_claim(claims, "name"),
            scopes=list(token.scopes),
        )

    # --- TaskLists Resource (tasklists.*) ---

    @tool(READ)
    async def list_tasklists(
        max_results: Annotated[
            int | None,
            Field(
                ge=1,
                le=100,
                description="Maximum number of task lists per page (1-100, default 20).",
            ),
        ] = None,
        page_token: Annotated[
            str | None,
            Field(description="Pagination token from a previous list_tasklists call."),
        ] = None,
    ) -> TaskListsResponse:
        """List all of the authenticated user's Google Task lists (tasklists.list).

        Call this first to discover task list IDs. Returns one page; when
        ``next_page_token`` is set, call again with it as ``page_token``.
        """
        return await api_factory().list_tasklists(max_results=max_results, page_token=page_token)

    @tool(READ)
    async def get_tasklist(
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
    ) -> TaskList:
        """Retrieve a single Google Task list by ID (tasklists.get).

        Fails with a 404 API error if the ID does not exist; use list_tasklists
        to find valid IDs.
        """
        return await api_factory().get_tasklist(tasklist_id)

    @tool(WRITE)
    async def create_tasklist(
        title: Annotated[
            str,
            Field(min_length=1, max_length=1024, description="Title of the new list."),
        ],
    ) -> TaskList:
        """Create a new Google Task list (tasklists.insert).

        Returns the new list, including the ID to use with the task tools.
        """
        return await api_factory().insert_tasklist(title)

    @tool(IDEMPOTENT_WRITE)
    async def update_tasklist(
        tasklist_id: Annotated[str, Field(description="Task list ID to replace (or '@default').")],
        title: Annotated[
            str,
            Field(min_length=1, max_length=1024, description="New task list title."),
        ],
    ) -> TaskList:
        """Replace a Google Task list's metadata via PUT (tasklists.update).

        Returns the updated list. To change only the title, patch_tasklist is equivalent.
        """
        return await api_factory().update_tasklist(tasklist_id, title)

    @tool(IDEMPOTENT_WRITE)
    async def patch_tasklist(
        tasklist_id: Annotated[str, Field(description="Task list ID to patch (or '@default').")],
        title: Annotated[
            str,
            Field(min_length=1, max_length=1024, description="Updated task list title."),
        ],
    ) -> TaskList:
        """Partially update a Google Task list via PATCH (tasklists.patch).

        Returns the updated list.
        """
        return await api_factory().patch_tasklist(tasklist_id, title)

    @tool(DESTRUCTIVE)
    async def delete_tasklist(
        tasklist_id: Annotated[str, Field(description="ID of the task list to delete.")],
    ) -> OperationResult:
        """Delete a Google Task list and all of its tasks (tasklists.delete).

        This cannot be undone. Confirm with the user before calling.
        """
        await api_factory().delete_tasklist(tasklist_id)
        return OperationResult(message=f"Deleted task list '{tasklist_id}'.")

    # --- Tasks Resource (tasks.*) ---

    @tool(READ)
    async def list_tasks(
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
        completed_max: Annotated[
            str | None,
            Field(description="Upper bound for completion date (RFC 3339 timestamp)."),
        ] = None,
        completed_min: Annotated[
            str | None,
            Field(description="Lower bound for completion date (RFC 3339 timestamp)."),
        ] = None,
        due_max: Annotated[
            str | None,
            Field(description="Upper bound for due date (RFC 3339 timestamp)."),
        ] = None,
        due_min: Annotated[
            str | None,
            Field(description="Lower bound for due date (RFC 3339 timestamp)."),
        ] = None,
        max_results: Annotated[
            int | None,
            Field(
                ge=1,
                le=100,
                description="Maximum number of tasks per page (1-100, default 20).",
            ),
        ] = None,
        page_token: Annotated[
            str | None,
            Field(description="Pagination token from a previous list_tasks call."),
        ] = None,
        show_assigned: Annotated[
            bool | None,
            Field(description="Include tasks assigned to the user from Docs/Chat."),
        ] = None,
        show_completed: Annotated[
            bool | None,
            Field(
                description=(
                    "Include completed tasks (default True). Note: set show_hidden=True "
                    "as well to include tasks completed in Google's web/mobile apps."
                ),
            ),
        ] = None,
        show_deleted: Annotated[
            bool | None,
            Field(description="Include deleted tasks (default False)."),
        ] = None,
        show_hidden: Annotated[
            bool | None,
            Field(description="Include hidden tasks (default False)."),
        ] = None,
        updated_min: Annotated[
            str | None,
            Field(description="Lower bound for last modification time (RFC 3339)."),
        ] = None,
    ) -> TasksResponse:
        """List tasks in a Google Task list with full filtering (tasks.list).

        Returns one page; when ``next_page_token`` is set, call again with it as
        ``page_token``. Timestamps must be RFC 3339 with a timezone
        (e.g. '2026-11-22T00:00:00Z'); a malformed one is rejected before any call.
        """
        optional: dict[str, QueryParamValue | None] = {
            "completedMax": check_rfc3339(completed_max, "completed_max"),
            "completedMin": check_rfc3339(completed_min, "completed_min"),
            "dueMax": check_rfc3339(due_max, "due_max"),
            "dueMin": check_rfc3339(due_min, "due_min"),
            "maxResults": max_results,
            "pageToken": page_token,
            "showAssigned": show_assigned,
            "showCompleted": show_completed,
            "showDeleted": show_deleted,
            "showHidden": show_hidden,
            "updatedMin": check_rfc3339(updated_min, "updated_min"),
        }
        query = {key: value for key, value in optional.items() if value is not None}
        return await api_factory().list_tasks(tasklist_id, query=query)

    @tool(READ)
    async def get_task(
        task_id: Annotated[str, Field(description="Task identifier.")],
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
    ) -> Task:
        """Retrieve a single task by ID (tasks.get).

        Fails with a 404 API error if the task is not in that list; use list_tasks
        to find valid IDs.
        """
        return await api_factory().get_task(tasklist_id, task_id)

    @tool(WRITE)
    async def create_task(
        title: Annotated[str, Field(max_length=1024, description="Title of the task.")],
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
        notes: Annotated[
            str | None,
            Field(max_length=8192, description="Optional notes describing the task."),
        ] = None,
        due: Annotated[
            str | None,
            Field(
                description=(
                    "Due date as RFC 3339 timestamp (e.g. '2026-12-01T00:00:00Z'). "
                    "Google Tasks stores the date portion only."
                ),
            ),
        ] = None,
        status: Annotated[
            TaskStatus | None,
            Field(description="Task status: 'needsAction' or 'completed'."),
        ] = None,
        completed: Annotated[
            str | None,
            Field(description="Completion date as RFC 3339 timestamp."),
        ] = None,
        parent: Annotated[
            str | None,
            Field(description="Parent task ID to create this task as a subtask."),
        ] = None,
        previous: Annotated[
            str | None,
            Field(description="Previous sibling task ID to position after."),
        ] = None,
    ) -> Task:
        """Create a new task in a Google Task list (tasks.insert).

        Returns the created task with its ID. Timestamps must be RFC 3339 with a
        timezone; a malformed one is rejected before any call.
        """
        body: RequestBody = {"title": title}
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = check_rfc3339(due, "due")
        if status is not None:
            body["status"] = status
        if completed is not None:
            body["completed"] = check_rfc3339(completed, "completed")
        return await api_factory().insert_task(tasklist_id, body, parent=parent, previous=previous)

    @tool(IDEMPOTENT_WRITE)
    async def update_task(
        task_id: Annotated[str, Field(description="Task identifier.")],
        title: Annotated[str, Field(max_length=1024, description="Task title.")],
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
        notes: Annotated[
            str | None,
            Field(max_length=8192, description="Task notes."),
        ] = None,
        due: Annotated[
            str | None,
            Field(description="Due date as RFC 3339 timestamp."),
        ] = None,
        status: Annotated[
            TaskStatus,
            Field(description="Task status: 'needsAction' or 'completed'."),
        ] = "needsAction",
        completed: Annotated[
            str | None,
            Field(description="Completion date as RFC 3339 timestamp."),
        ] = None,
        deleted: Annotated[
            bool | None,
            Field(description="Whether the task is marked as deleted."),
        ] = None,
        hidden: Annotated[
            bool | None,
            Field(description="Whether the task is hidden."),
        ] = None,
    ) -> Task:
        """Replace a task's fields via PUT (tasks.update).

        Any omitted optional fields may be cleared by Google Tasks. For partial
        updates, prefer ``patch_task``.
        """
        body: RequestBody = {"id": task_id, "title": title, "status": status}
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = check_rfc3339(due, "due")
        if completed is not None:
            body["completed"] = check_rfc3339(completed, "completed")
        if deleted is not None:
            body["deleted"] = deleted
        if hidden is not None:
            body["hidden"] = hidden
        return await api_factory().update_task(tasklist_id, task_id, body)

    @tool(IDEMPOTENT_WRITE)
    async def patch_task(
        task_id: Annotated[str, Field(description="Task identifier.")],
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
        title: Annotated[
            str | None,
            Field(max_length=1024, description="Updated task title."),
        ] = None,
        notes: Annotated[
            str | None,
            Field(max_length=8192, description="Updated task notes."),
        ] = None,
        due: Annotated[
            str | None,
            Field(description="Updated due date as RFC 3339 timestamp."),
        ] = None,
        status: Annotated[
            TaskStatus | None,
            Field(description="Updated status: 'needsAction' or 'completed'."),
        ] = None,
        completed: Annotated[
            str | None,
            Field(description="Updated completion date as RFC 3339 timestamp."),
        ] = None,
        deleted: Annotated[
            bool | None,
            Field(description="Updated deleted flag."),
        ] = None,
        hidden: Annotated[
            bool | None,
            Field(description="Updated hidden flag."),
        ] = None,
    ) -> Task:
        """Partially update a task via PATCH (tasks.patch).

        Only provided fields are modified. Setting ``status='needsAction'``
        automatically clears the ``completed`` timestamp unless explicitly set.
        Use this to mark a task done (``status='completed'``). Fails if no field
        is given.
        """
        body: RequestBody = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = check_rfc3339(due, "due")
        if status is not None:
            body["status"] = status
            if status == "needsAction" and completed is None:
                body["completed"] = None
        if completed is not None:
            body["completed"] = check_rfc3339(completed, "completed")
        if deleted is not None:
            body["deleted"] = deleted
        if hidden is not None:
            body["hidden"] = hidden
        if not body:
            msg = "At least one field must be provided to patch_task."
            raise InvalidArgumentError(msg)
        return await api_factory().patch_task(tasklist_id, task_id, body)

    @tool(DESTRUCTIVE)
    async def delete_task(
        task_id: Annotated[str, Field(description="Task identifier to delete.")],
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
    ) -> OperationResult:
        """Delete a task from a Google Task list (tasks.delete).

        Confirm with the user before calling. To finish a task instead, use
        patch_task with ``status='completed'``.
        """
        await api_factory().delete_task(tasklist_id, task_id)
        return OperationResult(message=f"Deleted task '{task_id}' from task list '{tasklist_id}'.")

    @tool(WRITE)
    async def move_task(
        task_id: Annotated[str, Field(description="Task identifier to move.")],
        tasklist_id: Annotated[
            str,
            Field(description="Source task list ID, or '@default'."),
        ] = "@default",
        parent: Annotated[
            str | None,
            Field(
                description=(
                    "New parent task ID to make this a subtask. Omit to move to the top level."
                ),
            ),
        ] = None,
        previous: Annotated[
            str | None,
            Field(description="New previous sibling task ID. Omit to move to first position."),
        ] = None,
        destination_tasklist: Annotated[
            str | None,
            Field(
                description=(
                    "Destination task list ID if moving the task to a different "
                    "list. Omit to reorder within the current list."
                ),
            ),
        ] = None,
    ) -> Task:
        """Move or reparent a task within or across task lists (tasks.move).

        Returns the task at its new position.
        """
        return await api_factory().move_task(
            tasklist_id,
            task_id,
            parent=parent,
            previous=previous,
            destination_tasklist=destination_tasklist,
        )

    @tool(DESTRUCTIVE)
    async def clear_completed_tasks(
        tasklist_id: Annotated[str, Field(description=_TASKLIST_DEFAULT_DESC)] = "@default",
    ) -> OperationResult:
        """Clear all completed tasks from a task list (tasks.clear).

        Affected tasks are marked as hidden and will no longer appear in
        default task listings.
        """
        await api_factory().clear_tasks(tasklist_id)
        return OperationResult(message=f"Cleared completed tasks from task list '{tasklist_id}'.")


def register_resources(mcp: FastMCP, api_factory: ApiFactory) -> None:
    """Register read-only MCP resources for browsing task lists and tasks.

    Both resources read every page (up to ``api.MAX_PAGES``) rather than only
    the first.

    Args:
        mcp: The server to add resources to.
        api_factory: Returns the Tasks API to use for the current read.
    """

    @mcp.resource("gtasks://lists", mime_type="application/json")
    @_anticipated_resource
    async def resource_tasklists() -> str:
        """Return all Google Task lists for the authenticated user as JSON."""
        model = await fetch_all_tasklists(api_factory())
        return model.model_dump_json(indent=2)

    @mcp.resource("gtasks://lists/{tasklist_id}/tasks", mime_type="application/json")
    @_anticipated_resource
    async def resource_tasks(tasklist_id: str) -> str:
        """Return tasks in the specified Google Task list as JSON."""
        model = await fetch_all_tasks(api_factory(), tasklist_id)
        return model.model_dump_json(indent=2)
