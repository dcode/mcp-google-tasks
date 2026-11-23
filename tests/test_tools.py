"""Tests for the MCP tools and resources, driven through the in-memory FastMCP client."""

import json
import re
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.shared.exceptions import MCPError
from mcp.types import TextResourceContents

from mcp_google_tasks.config import AuthMode
from mcp_google_tasks.errors import ApiError, InvalidArgumentError
from mcp_google_tasks.tools import anticipated, check_rfc3339

from .conftest import (
    CLIENT_ID,
    DEFAULT_LIST_ID,
    FakeGoogle,
    ServerFactory,
    SettingsFactory,
    call_tool,
    google_error,
    tool_error,
)

READ_TOOLS = frozenset(
    {"get_current_user", "list_tasklists", "get_tasklist", "list_tasks", "get_task"}
)
WRITE_TOOLS = frozenset(
    {
        "create_tasklist",
        "update_tasklist",
        "patch_tasklist",
        "delete_tasklist",
        "create_task",
        "update_task",
        "patch_task",
        "delete_task",
        "move_task",
        "clear_completed_tasks",
    }
)
DUE = "2026-12-01T00:00:00Z"
DONE = "2026-11-30T12:00:00+00:00"

type McpClient = Client[FastMCPTransport]


async def _read_json(client: McpClient, uri: str) -> Any:
    """Read a resource that must succeed and decode its single JSON block."""
    contents = await client.read_resource(uri)
    assert len(contents) == 1
    block = contents[0]
    assert isinstance(block, TextResourceContents)
    assert block.mime_type == "application/json"
    return json.loads(block.text)


# --- Registration and annotations -------------------------------------------


async def test_all_tools_are_listed_with_annotations(mcp_client: McpClient) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False


@pytest.mark.parametrize(
    ("name", "destructive", "idempotent"),
    [
        ("delete_tasklist", True, None),
        ("delete_task", True, None),
        ("clear_completed_tasks", True, None),
        ("update_task", None, True),
        ("patch_task", None, True),
        ("update_tasklist", None, True),
        ("patch_tasklist", None, True),
        ("create_task", None, None),
        ("move_task", None, None),
    ],
)
async def test_write_tool_hints_match_original(
    mcp_client: McpClient, name: str, destructive: bool | None, idempotent: bool | None
) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}
    annotations = tools[name].annotations
    assert annotations is not None
    assert annotations.destructive_hint is destructive
    assert annotations.idempotent_hint is idempotent
    assert annotations.open_world_hint is True


async def test_readonly_registers_only_read_tools(
    make_server: ServerFactory, make_settings: SettingsFactory
) -> None:
    server = make_server(make_settings("token", GOOGLE_TASKS_READONLY="true"))
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
        lists = await call_tool(client, "list_tasklists")
        missing = await client.call_tool("create_task", {"title": "x"}, raise_on_error=False)

    assert names == READ_TOOLS
    assert lists["items"][0]["id"] == DEFAULT_LIST_ID
    assert missing.is_error


async def test_resources_are_registered(mcp_client: McpClient) -> None:
    resources = await mcp_client.list_resources()
    templates = await mcp_client.list_resource_templates()

    assert [str(r.uri) for r in resources] == ["gtasks://lists"]
    assert [t.uri_template for t in templates] == ["gtasks://lists/{tasklist_id}/tasks"]


# --- get_current_user ----------------------------------------------------------


async def test_get_current_user_token_mode_uses_userinfo(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    user = await call_tool(mcp_client, "get_current_user")

    assert user == {
        "sub": "1234567890",
        "email": "user@example.com",
        "name": "Test User",
        "scopes": [],
    }
    assert fake_google.last.path == "/oauth2/v3/userinfo"


async def test_get_current_user_token_mode_reports_userinfo_failure(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.fail_next(500, "boom", match="/userinfo")

    message = await tool_error(mcp_client, "get_current_user")

    assert "500" in message


@pytest.mark.parametrize("auth_mode", ["oauth-proxy", "resource-server"])
async def test_get_current_user_reads_token_claims(
    make_server: ServerFactory,
    make_settings: SettingsFactory,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: AuthMode,
) -> None:
    token = AccessToken(
        token="upstream-token",  # noqa: S106 - test placeholder
        client_id=CLIENT_ID,
        scopes=["openid", "https://www.googleapis.com/auth/tasks"],
        claims={"sub": 42, "email": "user@example.com", "name": None},
    )
    monkeypatch.setattr("mcp_google_tasks.tools.get_access_token", lambda: token)
    server = make_server(make_settings(auth_mode))

    async with Client(server) as client:
        user = await call_tool(client, "get_current_user")

    assert user == {
        "sub": "42",
        "email": "user@example.com",
        "name": None,
        "scopes": ["openid", "https://www.googleapis.com/auth/tasks"],
    }
    assert fake_google.requests == []


async def test_get_current_user_without_request_token_fails(
    make_server: ServerFactory,
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mcp_google_tasks.tools.get_access_token", lambda: None)
    server = make_server(make_settings("oauth-proxy"))

    async with Client(server) as client:
        message = await tool_error(client, "get_current_user")

    assert "No authenticated Google OAuth access token" in message


# --- Task lists ------------------------------------------------------------------


async def test_list_tasklists_pages(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_tasklist("Work", tasklist_id="work")

    first = await call_tool(mcp_client, "list_tasklists", {"max_results": 1})
    second = await call_tool(
        mcp_client, "list_tasklists", {"max_results": 1, "page_token": first["next_page_token"]}
    )

    assert [tl["id"] for tl in first["items"]] == [DEFAULT_LIST_ID]
    assert [tl["id"] for tl in second["items"]] == ["work"]
    assert second["next_page_token"] is None
    assert fake_google.last.params == {"maxResults": "1", "pageToken": "1"}


async def test_list_tasklists_rejects_out_of_range_max_results(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    result = await mcp_client.call_tool(
        "list_tasklists", {"max_results": 101}, raise_on_error=False
    )

    assert result.is_error
    assert fake_google.requests == []


@pytest.mark.parametrize(
    ("arguments", "expected_id"),
    [({}, DEFAULT_LIST_ID), ({"tasklist_id": "work"}, "work")],
)
async def test_get_tasklist(
    mcp_client: McpClient,
    fake_google: FakeGoogle,
    arguments: dict[str, str],
    expected_id: str,
) -> None:
    fake_google.add_tasklist("Work", tasklist_id="work")

    tasklist = await call_tool(mcp_client, "get_tasklist", arguments)

    assert tasklist["id"] == expected_id
    assert tasklist["self_link"].endswith(f"/lists/{expected_id}")


async def test_get_tasklist_not_found(mcp_client: McpClient) -> None:
    message = await tool_error(mcp_client, "get_tasklist", {"tasklist_id": "missing"})

    assert message == "Google Tasks API error 404 (NOT_FOUND): Requested entity was not found."


async def test_create_tasklist(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    tasklist = await call_tool(mcp_client, "create_tasklist", {"title": "Groceries"})

    assert fake_google.tasklists[tasklist["id"]]["title"] == "Groceries"
    assert (fake_google.last.method, fake_google.last.json) == ("POST", {"title": "Groceries"})


async def test_create_tasklist_rejects_empty_title(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    result = await mcp_client.call_tool("create_tasklist", {"title": ""}, raise_on_error=False)

    assert result.is_error
    assert fake_google.requests == []


@pytest.mark.parametrize(
    ("name", "method"), [("update_tasklist", "PUT"), ("patch_tasklist", "PATCH")]
)
async def test_rename_tasklist(
    mcp_client: McpClient, fake_google: FakeGoogle, name: str, method: str
) -> None:
    tasklist = await call_tool(
        mcp_client, name, {"tasklist_id": DEFAULT_LIST_ID, "title": "Renamed"}
    )

    assert tasklist["title"] == "Renamed"
    assert fake_google.tasklists[DEFAULT_LIST_ID]["title"] == "Renamed"
    assert fake_google.last.method == method


async def test_delete_tasklist(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_tasklist("Old", tasklist_id="old")

    result = await call_tool(mcp_client, "delete_tasklist", {"tasklist_id": "old"})

    assert result == {"status": "ok", "message": "Deleted task list 'old'."}
    assert "old" not in fake_google.tasklists


async def test_delete_tasklist_failure(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.fail_next(
        403, google_error(403, "Forbidden.", "PERMISSION_DENIED"), method="DELETE"
    )

    message = await tool_error(mcp_client, "delete_tasklist", {"tasklist_id": DEFAULT_LIST_ID})

    assert message == "Google Tasks API error 403 (PERMISSION_DENIED): Forbidden."
    assert DEFAULT_LIST_ID in fake_google.tasklists


# --- list_tasks ------------------------------------------------------------------


async def test_list_tasks_defaults_send_no_filters(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task("@default", "Open", task_id="open")
    fake_google.add_task("@default", "Done", task_id="done", status="completed", hidden=True)

    result = await call_tool(mcp_client, "list_tasks")

    assert [task["id"] for task in result["items"]] == ["open"]
    assert fake_google.last.path == "/tasks/v1/lists/@default/tasks"
    assert fake_google.last.params == {}


async def test_list_tasks_passes_every_filter(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task("@default", "Open", task_id="open")
    fake_google.add_task("@default", "Done", task_id="done", status="completed", hidden=True)

    result = await call_tool(
        mcp_client,
        "list_tasks",
        {
            "tasklist_id": DEFAULT_LIST_ID,
            "completed_max": " 2026-12-31T00:00:00Z ",
            "completed_min": "2026-01-01T00:00:00z",
            "due_max": "2026-12-31T00:00:00+02:00",
            "due_min": "2026-01-01T00:00:00.000Z",
            "max_results": 50,
            "page_token": "0",
            "show_assigned": True,
            "show_completed": True,
            "show_deleted": False,
            "show_hidden": True,
            "updated_min": "2026-01-01T00:00:00-05:00",
        },
    )

    assert [task["id"] for task in result["items"]] == ["open", "done"]
    assert fake_google.last.params == {
        "completedMax": "2026-12-31T00:00:00Z",
        "completedMin": "2026-01-01T00:00:00z",
        "dueMax": "2026-12-31T00:00:00+02:00",
        "dueMin": "2026-01-01T00:00:00.000Z",
        "maxResults": "50",
        "pageToken": "0",
        "showAssigned": "true",
        "showCompleted": "true",
        "showDeleted": "false",
        "showHidden": "true",
        "updatedMin": "2026-01-01T00:00:00-05:00",
    }


async def test_list_tasks_returns_next_page_token(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    for title in ("a", "b", "c"):
        fake_google.add_task("@default", title)

    result = await call_tool(mcp_client, "list_tasks", {"max_results": 2})

    assert len(result["items"]) == 2
    assert result["next_page_token"] == "2"  # noqa: S105 - a page offset, not a secret


@pytest.mark.parametrize(
    "argument", ["completed_max", "completed_min", "due_max", "due_min", "updated_min"]
)
@pytest.mark.parametrize("value", ["tomorrow", "2026-12-01T00:00:00", "", "Z"])
async def test_list_tasks_rejects_bad_timestamp_before_calling_google(
    mcp_client: McpClient, fake_google: FakeGoogle, argument: str, value: str
) -> None:
    message = await tool_error(mcp_client, "list_tasks", {argument: value})

    assert message.startswith(f"{argument} must be an RFC 3339 timestamp with a timezone")
    assert repr(value) in message
    assert fake_google.requests == []


async def test_list_tasks_unknown_list(mcp_client: McpClient) -> None:
    message = await tool_error(mcp_client, "list_tasks", {"tasklist_id": "nope"})

    assert "404" in message


# --- get_task ----------------------------------------------------------------------


async def test_get_task(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_task("@default", "Read", task_id="t1", notes="chapter 3", due=DUE)

    task = await call_tool(mcp_client, "get_task", {"task_id": "t1"})

    assert (task["id"], task["title"], task["notes"], task["due"]) == (
        "t1",
        "Read",
        "chapter 3",
        DUE,
    )
    assert task["web_view_link"] == "https://tasks.example.com/task/t1"
    assert fake_google.last.path == "/tasks/v1/lists/@default/tasks/t1"


async def test_get_task_not_found(mcp_client: McpClient) -> None:
    message = await tool_error(mcp_client, "get_task", {"task_id": "nope"})

    assert message == "Google Tasks API error 404 (NOT_FOUND): Requested entity was not found."


async def test_get_task_rejects_path_traversal_id(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    message = await tool_error(mcp_client, "get_task", {"task_id": ".."})

    assert "task" in message.lower()
    assert fake_google.requests == []


async def test_tool_reports_connectivity_failure(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.raise_next()

    message = await tool_error(mcp_client, "get_task", {"task_id": "t1"})

    assert message == (
        "Could not reach Google (ConnectError). Check network connectivity and try again."
    )


async def test_tool_retries_after_token_refresh(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task("@default", "Read", task_id="t1")
    fake_google.expire_access_tokens()

    task = await call_tool(mcp_client, "get_task", {"task_id": "t1"})

    assert task["id"] == "t1"
    assert len(fake_google.token_requests) == 1
    assert fake_google.last.authorization == "Bearer refreshed-token-1"


# --- create_task -------------------------------------------------------------------


async def test_create_task_minimal(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    task = await call_tool(mcp_client, "create_task", {"title": "Buy milk"})

    stored = fake_google.task("@default", task["id"])
    assert stored is not None
    assert stored["title"] == "Buy milk"
    assert fake_google.last.json == {"title": "Buy milk"}
    assert fake_google.last.params == {}


async def test_create_task_with_every_field(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_tasklist("Work", tasklist_id="work")
    fake_google.add_task("work", "Parent", task_id="p")
    fake_google.add_task("work", "Sibling", task_id="s", parent="p")

    task = await call_tool(
        mcp_client,
        "create_task",
        {
            "title": "Child",
            "tasklist_id": "work",
            "notes": "details",
            "due": f" {DUE} ",
            "status": "completed",
            "completed": DONE,
            "parent": "p",
            "previous": "s",
        },
    )

    assert fake_google.last.path == "/tasks/v1/lists/work/tasks"
    assert fake_google.last.params == {"parent": "p", "previous": "s"}
    assert fake_google.last.json == {
        "title": "Child",
        "notes": "details",
        "due": DUE,
        "status": "completed",
        "completed": DONE,
    }
    assert task["parent"] == "p"
    assert [t["id"] for t in fake_google.tasks["work"]] == ["p", "s", task["id"]]


@pytest.mark.parametrize("argument", ["due", "completed"])
async def test_create_task_rejects_bad_timestamp(
    mcp_client: McpClient, fake_google: FakeGoogle, argument: str
) -> None:
    message = await tool_error(mcp_client, "create_task", {"title": "x", argument: "next week"})

    assert message.startswith(f"{argument} must be an RFC 3339 timestamp")
    assert fake_google.requests == []


async def test_create_task_rejects_unknown_status(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    result = await mcp_client.call_tool(
        "create_task", {"title": "x", "status": "done"}, raise_on_error=False
    )

    assert result.is_error
    assert fake_google.requests == []


async def test_create_task_encodes_list_id(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_tasklist("Odd", tasklist_id="a/b")

    task = await call_tool(mcp_client, "create_task", {"title": "x/y", "tasklist_id": "a/b"})

    assert task["title"] == "x/y"
    assert fake_google.last.path == "/tasks/v1/lists/a%2Fb/tasks"


# --- update_task -------------------------------------------------------------------


async def test_update_task_minimal_sends_defaults(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task("@default", "Old", task_id="t1", notes="gone after PUT")

    task = await call_tool(mcp_client, "update_task", {"task_id": "t1", "title": "New"})

    assert fake_google.last.method == "PUT"
    assert fake_google.last.json == {"id": "t1", "title": "New", "status": "needsAction"}
    assert task["title"] == "New"
    assert task["notes"] is None


async def test_update_task_with_every_field(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_task("@default", "Old", task_id="t1")

    task = await call_tool(
        mcp_client,
        "update_task",
        {
            "task_id": "t1",
            "title": "New",
            "tasklist_id": DEFAULT_LIST_ID,
            "notes": "n",
            "due": DUE,
            "status": "completed",
            "completed": DONE,
            "deleted": False,
            "hidden": True,
        },
    )

    assert fake_google.last.json == {
        "id": "t1",
        "title": "New",
        "status": "completed",
        "notes": "n",
        "due": DUE,
        "completed": DONE,
        "deleted": False,
        "hidden": True,
    }
    assert (task["status"], task["hidden"]) == ("completed", True)


@pytest.mark.parametrize("argument", ["due", "completed"])
async def test_update_task_rejects_bad_timestamp(
    mcp_client: McpClient, fake_google: FakeGoogle, argument: str
) -> None:
    message = await tool_error(
        mcp_client, "update_task", {"task_id": "t1", "title": "x", argument: "2026-13-01T00:00:00Z"}
    )

    assert message.startswith(f"{argument} must be an RFC 3339 timestamp")
    assert fake_google.requests == []


async def test_update_task_not_found(mcp_client: McpClient) -> None:
    message = await tool_error(mcp_client, "update_task", {"task_id": "nope", "title": "x"})

    assert "404" in message


# --- patch_task --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "body"),
    [
        ({"title": "T"}, {"title": "T"}),
        ({"notes": "N"}, {"notes": "N"}),
        ({"due": DUE}, {"due": DUE}),
        ({"status": "completed"}, {"status": "completed"}),
        ({"status": "needsAction"}, {"status": "needsAction", "completed": None}),
        (
            {"status": "needsAction", "completed": DONE},
            {"status": "needsAction", "completed": DONE},
        ),
        ({"completed": DONE}, {"completed": DONE}),
        ({"deleted": True}, {"deleted": True}),
        ({"hidden": False}, {"hidden": False}),
    ],
)
async def test_patch_task_sends_only_given_fields(
    mcp_client: McpClient,
    fake_google: FakeGoogle,
    arguments: dict[str, Any],
    body: dict[str, Any],
) -> None:
    fake_google.add_task("@default", "Old", task_id="t1")

    await call_tool(mcp_client, "patch_task", {"task_id": "t1", **arguments})

    assert fake_google.last.method == "PATCH"
    assert fake_google.last.json == body


async def test_patch_task_reopening_clears_completed(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task("@default", "Done", task_id="t1", status="completed", completed=DONE)

    task = await call_tool(mcp_client, "patch_task", {"task_id": "t1", "status": "needsAction"})

    assert task["status"] == "needsAction"
    assert task["completed"] is None
    stored = fake_google.task("@default", "t1")
    assert stored is not None
    assert "completed" not in stored


async def test_patch_task_without_fields_fails_before_calling_google(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    message = await tool_error(mcp_client, "patch_task", {"task_id": "t1"})

    assert message == "At least one field must be provided to patch_task."
    assert fake_google.requests == []


@pytest.mark.parametrize("argument", ["due", "completed"])
async def test_patch_task_rejects_bad_timestamp(
    mcp_client: McpClient, fake_google: FakeGoogle, argument: str
) -> None:
    message = await tool_error(mcp_client, "patch_task", {"task_id": "t1", argument: "soon"})

    assert message.startswith(f"{argument} must be an RFC 3339 timestamp")
    assert fake_google.requests == []


async def test_patch_task_server_error(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.fail_next(500, "oops", method="PATCH")

    message = await tool_error(mcp_client, "patch_task", {"task_id": "t1", "title": "x"})

    assert message == "Google Tasks API error 500: oops"


# --- delete_task, move_task, clear_completed_tasks ---------------------------------


async def test_delete_task(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_task("@default", "Bye", task_id="t1")

    result = await call_tool(mcp_client, "delete_task", {"task_id": "t1"})

    assert result == {
        "status": "ok",
        "message": "Deleted task 't1' from task list '@default'.",
    }
    assert fake_google.task("@default", "t1") is None


async def test_delete_task_not_found(mcp_client: McpClient) -> None:
    message = await tool_error(
        mcp_client, "delete_task", {"task_id": "nope", "tasklist_id": DEFAULT_LIST_ID}
    )

    assert "404" in message


async def test_move_task_to_top_level_by_default(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task("@default", "Parent", task_id="p")
    fake_google.add_task("@default", "Child", task_id="c", parent="p")

    task = await call_tool(mcp_client, "move_task", {"task_id": "c"})

    assert task["parent"] is None
    assert fake_google.last.path == "/tasks/v1/lists/@default/tasks/c/move"
    assert fake_google.last.params == {}
    assert [t["id"] for t in fake_google.tasks[DEFAULT_LIST_ID]] == ["c", "p"]


async def test_move_task_across_lists(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_tasklist("Work", tasklist_id="work")
    fake_google.add_task("work", "Parent", task_id="p")
    fake_google.add_task("work", "Sibling", task_id="s", parent="p")
    fake_google.add_task("@default", "Mover", task_id="m")

    task = await call_tool(
        mcp_client,
        "move_task",
        {
            "task_id": "m",
            "tasklist_id": DEFAULT_LIST_ID,
            "parent": "p",
            "previous": "s",
            "destination_tasklist": "work",
        },
    )

    assert task["parent"] == "p"
    assert fake_google.last.params == {
        "parent": "p",
        "previous": "s",
        "destinationTasklist": "work",
    }
    assert fake_google.tasks[DEFAULT_LIST_ID] == []
    assert [t["id"] for t in fake_google.tasks["work"]] == ["p", "s", "m"]


async def test_move_task_not_found(mcp_client: McpClient) -> None:
    message = await tool_error(mcp_client, "move_task", {"task_id": "nope"})

    assert "404" in message


async def test_clear_completed_tasks(mcp_client: McpClient, fake_google: FakeGoogle) -> None:
    fake_google.add_task("@default", "Open", task_id="open")
    fake_google.add_task("@default", "Done", task_id="done", status="completed")

    result = await call_tool(mcp_client, "clear_completed_tasks")
    remaining = await call_tool(mcp_client, "list_tasks")

    assert result == {
        "status": "ok",
        "message": "Cleared completed tasks from task list '@default'.",
    }
    assert [t["id"] for t in remaining["items"]] == ["open"]
    done = fake_google.task("@default", "done")
    assert done is not None
    assert done["hidden"] is True


async def test_clear_completed_tasks_failure(mcp_client: McpClient) -> None:
    message = await tool_error(mcp_client, "clear_completed_tasks", {"tasklist_id": "nope"})

    assert "404" in message


# --- Resources ---------------------------------------------------------------------


async def test_tasklists_resource_reads_every_page(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    for index in range(120):
        fake_google.add_tasklist(f"List {index}")

    data = await _read_json(mcp_client, "gtasks://lists")

    assert len(data["items"]) == 121
    assert data["items"][0]["id"] == DEFAULT_LIST_ID
    assert [r.params.get("pageToken") for r in fake_google.api_requests] == [None, "100"]


async def test_tasks_resource_reads_every_page(
    mcp_client: McpClient, fake_google: FakeGoogle
) -> None:
    for index in range(101):
        fake_google.add_task("@default", f"Task {index}")

    data = await _read_json(mcp_client, f"gtasks://lists/{DEFAULT_LIST_ID}/tasks")

    assert len(data["items"]) == 101
    assert data["items"][0]["title"] == "Task 0"
    assert {r.path for r in fake_google.api_requests} == {
        f"/tasks/v1/lists/{DEFAULT_LIST_ID}/tasks"
    }


@pytest.mark.parametrize(
    ("uri", "inject", "expected"),
    [
        (
            "gtasks://lists",
            True,
            "Google Tasks API error 503 (UNAVAILABLE): Try later.",
        ),
        (
            "gtasks://lists/missing/tasks",
            False,
            "Google Tasks API error 404 (NOT_FOUND): Requested entity was not found.",
        ),
    ],
)
async def test_resource_errors_reach_the_client(
    mcp_client: McpClient, fake_google: FakeGoogle, uri: str, inject: bool, expected: str
) -> None:
    if inject:
        fake_google.fail_next(503, google_error(503, "Try later.", "UNAVAILABLE"))

    with pytest.raises(MCPError, match=re.escape(expected)):
        await mcp_client.read_resource(uri)


# --- Helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2026-12-01T00:00:00Z", "2026-12-01T00:00:00Z"),
        ("2026-12-01T00:00:00z", "2026-12-01T00:00:00z"),
        ("  2026-12-01T00:00:00.123+05:30\n", "2026-12-01T00:00:00.123+05:30"),
        ("2026-12-01T00:00:00-08:00", "2026-12-01T00:00:00-08:00"),
    ],
)
def test_check_rfc3339_accepts_timezone_aware_values(
    value: str | None, expected: str | None
) -> None:
    assert check_rfc3339(value, "due") == expected


@pytest.mark.parametrize("value", ["", "   ", "Z", "2026-12-01", "2026-12-01T00:00:00", "nope"])
def test_check_rfc3339_rejects(value: str) -> None:
    with pytest.raises(InvalidArgumentError, match=r"^due_min must be an RFC 3339 timestamp"):
        check_rfc3339(value, "due_min")


@pytest.mark.parametrize(
    "error",
    [ApiError(409, "Conflict.", "ABORTED"), ValueError("bad value")],
)
async def test_anticipated_turns_expected_errors_into_tool_errors(error: Exception) -> None:
    async def fails() -> None:
        raise error

    with pytest.raises(ToolError, match=re.escape(str(error))) as excinfo:
        await anticipated(fails)()

    assert excinfo.value.__cause__ is error


async def test_anticipated_lets_unexpected_errors_through() -> None:
    async def fails() -> None:
        raise KeyError("internal")

    with pytest.raises(KeyError, match="internal"):
        await anticipated(fails)()


async def test_anticipated_keeps_return_value_and_name() -> None:
    async def answer(value: int) -> int:
        return value * 2

    wrapped = anticipated(answer)

    assert await wrapped(21) == 42
    assert getattr(wrapped, "__name__", None) == "answer"
