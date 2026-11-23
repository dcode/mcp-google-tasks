"""Tests for ``mcp_google_tasks.api``: the Google Tasks REST adapter and pagers."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from mcp_google_tasks.api import (
    GOOGLE_TASKS_API_BASE,
    MAX_PAGES,
    PAGE_SIZE,
    GoogleTasksClient,
    encode_segment,
    extract_google_error,
    fetch_all_tasklists,
    fetch_all_tasks,
)
from mcp_google_tasks.credentials import StaticCredentials
from mcp_google_tasks.errors import ApiError, ConnectivityError, InvalidArgumentError
from mcp_google_tasks.models import CurrentUserInfo

from .conftest import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_LIST_ID,
    REFRESH_TOKEN,
    TASKS_HOST,
    USERINFO_HOST,
    FakeGoogle,
    google_error,
)

LISTS = "/tasks/v1/users/@me/lists"
TASKS = f"/tasks/v1/lists/{DEFAULT_LIST_ID}/tasks"


def _credentials(http: httpx.AsyncClient, *, refresh: bool = True) -> StaticCredentials:
    return StaticCredentials(
        lambda: http,
        access_token=SecretStr(ACCESS_TOKEN),
        refresh_token=SecretStr(REFRESH_TOKEN) if refresh else None,
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
    )


@pytest.fixture
def api(http_client: httpx.AsyncClient) -> GoogleTasksClient:
    """A client over the fake with refreshable token-mode credentials."""
    return GoogleTasksClient(http_client, _credentials(http_client))


# --- encode_segment ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@default", "@default"),
        ("@me", "@me"),
        ("abc-123_XYZ", "abc-123_XYZ"),
        ("a/b", "a%2Fb"),
        ("a?b#c", "a%3Fb%23c"),
        ("x y", "x%20y"),
        ("@other", "%40other"),
        ("...", "..."),
        ("é", "%C3%A9"),
    ],
)
def test_encode_segment_encodes_everything_but_aliases(value: str, expected: str) -> None:
    assert encode_segment(value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "task_id must not be empty"),
        ("   ", "task_id must not be empty"),
        (".", "task_id must not be '.'"),
        ("..", "task_id must not be '..'"),
        ("a\nb", "task_id must not contain control characters"),
        ("a\x00", "task_id must not contain control characters"),
        ("\x7f", "task_id must not contain control characters"),
    ],
)
def test_encode_segment_rejects_unsafe_values(value: str, message: str) -> None:
    with pytest.raises(InvalidArgumentError, match=message):
        encode_segment(value, "task_id")


def test_encode_segment_error_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="identifier must not be empty"):
        encode_segment("")


# --- extract_google_error ----------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(404, json=google_error(404, "Not here.", "NOT_FOUND")),
            ("Not here.", "NOT_FOUND"),
        ),
        (
            httpx.Response(403, json={"error": {"code": 403}}),
            ("Forbidden", None),
        ),
        (
            httpx.Response(400, json={"error": {"message": "", "status": 7}}),
            ("Bad Request", "7"),
        ),
        (httpx.Response(400, json={"error": "invalid_request"}), ("invalid_request", None)),
        (httpx.Response(500, json=["unexpected"]), ('["unexpected"]', None)),
        (httpx.Response(500, json={"detail": "x"}), ('{"detail":"x"}', None)),
        (httpx.Response(500, json={"error": 42}), ('{"error":42}', None)),
        (httpx.Response(502, text="  upstream down \n"), ("upstream down", None)),
        (httpx.Response(503, text=""), ("Service Unavailable", None)),
        (httpx.Response(599, text=""), ("Unknown error", None)),
    ],
)
def test_extract_google_error(response: httpx.Response, expected: tuple[str, str | None]) -> None:
    assert extract_google_error(response) == expected


# --- Task list endpoints -----------------------------------------------------


async def test_list_tasklists_sends_no_query_by_default(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    page = await api.list_tasklists()
    assert [tl.id for tl in page.items] == [DEFAULT_LIST_ID]
    assert page.next_page_token is None
    request = fake_google.last
    assert (request.method, request.host, request.path) == ("GET", TASKS_HOST, LISTS)
    assert request.params == {}
    assert request.authorization == f"Bearer {ACCESS_TOKEN}"


async def test_list_tasklists_pages(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    for n in range(3):
        fake_google.add_tasklist(f"List {n}")
    first = await api.list_tasklists(max_results=2)
    assert len(first.items) == 2
    assert first.next_page_token == "2"  # noqa: S105 - a page offset, not a secret
    second = await api.list_tasklists(max_results=2, page_token=first.next_page_token)
    assert [tl.title for tl in second.items] == ["List 1", "List 2"]
    assert second.next_page_token is None
    assert fake_google.last.params == {"maxResults": "2", "pageToken": "2"}


async def test_get_tasklist(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    tasklist = await api.get_tasklist("@default")
    assert tasklist.id == DEFAULT_LIST_ID
    assert tasklist.title == "My Tasks"
    assert fake_google.last.path == f"{LISTS}/@default"


async def test_insert_tasklist(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    created = await api.insert_tasklist("Groceries")
    assert created.title == "Groceries"
    assert created.id in fake_google.tasklists
    assert (fake_google.last.method, fake_google.last.path) == ("POST", LISTS)
    assert fake_google.last.json == {"title": "Groceries"}


async def test_update_tasklist_sends_id_and_title(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    updated = await api.update_tasklist(DEFAULT_LIST_ID, "Renamed")
    assert updated.title == "Renamed"
    assert fake_google.tasklists[DEFAULT_LIST_ID]["title"] == "Renamed"
    assert fake_google.last.method == "PUT"
    assert fake_google.last.json == {"id": DEFAULT_LIST_ID, "title": "Renamed"}


async def test_patch_tasklist_sends_only_title(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    patched = await api.patch_tasklist(DEFAULT_LIST_ID, "Patched")
    assert patched.title == "Patched"
    assert fake_google.last.method == "PATCH"
    assert fake_google.last.json == {"title": "Patched"}


async def test_delete_tasklist(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    extra = fake_google.add_tasklist("Temp")
    await api.delete_tasklist(extra["id"])
    assert extra["id"] not in fake_google.tasklists
    assert (fake_google.last.method, fake_google.last.path) == ("DELETE", f"{LISTS}/{extra['id']}")


# --- Task endpoints ----------------------------------------------------------


async def test_list_tasks_renders_query_values(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_task(DEFAULT_LIST_ID, "Open")
    fake_google.add_task(DEFAULT_LIST_ID, "Done", status="completed")
    page = await api.list_tasks(
        DEFAULT_LIST_ID, query={"showCompleted": False, "showHidden": True, "maxResults": 5}
    )
    assert [t.title for t in page.items] == ["Open"]
    assert fake_google.last.path == TASKS
    assert fake_google.last.params == {
        "showCompleted": "false",
        "showHidden": "true",
        "maxResults": "5",
    }


async def test_list_tasks_with_empty_query_sends_no_params(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    page = await api.list_tasks("@default", query={})
    assert page.items == []
    assert fake_google.last.params == {}


async def test_get_task(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    seeded = fake_google.add_task(DEFAULT_LIST_ID, "Read", notes="n", due="2026-01-02T00:00:00Z")
    task = await api.get_task(DEFAULT_LIST_ID, seeded["id"])
    assert (task.id, task.title, task.notes) == (seeded["id"], "Read", "n")
    assert task.due == "2026-01-02T00:00:00Z"
    assert task.web_view_link == seeded["webViewLink"]
    assert fake_google.last.path == f"{TASKS}/{seeded['id']}"


async def test_insert_task_without_placement(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    task = await api.insert_task("@default", {"title": "New", "notes": None})
    assert task.title == "New"
    assert fake_google.last.method == "POST"
    assert fake_google.last.params == {}
    assert fake_google.last.json == {"title": "New", "notes": None}


async def test_insert_task_with_parent_and_previous(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    parent = fake_google.add_task(DEFAULT_LIST_ID, "Parent")
    sibling = fake_google.add_task(DEFAULT_LIST_ID, "Sibling", parent=parent["id"])
    task = await api.insert_task(
        DEFAULT_LIST_ID, {"title": "Child"}, parent=parent["id"], previous=sibling["id"]
    )
    assert task.parent == parent["id"]
    assert fake_google.last.params == {"parent": parent["id"], "previous": sibling["id"]}
    ids = [t["id"] for t in fake_google.tasks[DEFAULT_LIST_ID]]
    assert ids == [parent["id"], sibling["id"], task.id]


async def test_update_task(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    seeded = fake_google.add_task(DEFAULT_LIST_ID, "Old", notes="drop me")
    body: dict[str, str | bool | None] = {"id": seeded["id"], "title": "New", "status": "completed"}
    task = await api.update_task(DEFAULT_LIST_ID, seeded["id"], body)
    assert (task.title, task.status, task.notes) == ("New", "completed", None)
    assert fake_google.last.method == "PUT"
    assert fake_google.last.json == body


async def test_patch_task_can_clear_a_field(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    seeded = fake_google.add_task(DEFAULT_LIST_ID, "Keep", notes="clear me")
    task = await api.patch_task(DEFAULT_LIST_ID, seeded["id"], {"notes": None})
    assert (task.title, task.notes) == ("Keep", None)
    assert fake_google.last.method == "PATCH"
    assert fake_google.last.json == {"notes": None}


async def test_delete_task(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    seeded = fake_google.add_task(DEFAULT_LIST_ID, "Gone")
    await api.delete_task(DEFAULT_LIST_ID, seeded["id"])
    assert fake_google.task(DEFAULT_LIST_ID, seeded["id"]) is None
    assert (fake_google.last.method, fake_google.last.path) == ("DELETE", f"{TASKS}/{seeded['id']}")


async def test_move_task_without_options(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    first = fake_google.add_task(DEFAULT_LIST_ID, "First")
    second = fake_google.add_task(DEFAULT_LIST_ID, "Second")
    moved = await api.move_task(DEFAULT_LIST_ID, second["id"])
    assert moved.id == second["id"]
    assert [t["id"] for t in fake_google.tasks[DEFAULT_LIST_ID]] == [second["id"], first["id"]]
    assert (fake_google.last.method, fake_google.last.path) == (
        "POST",
        f"{TASKS}/{second['id']}/move",
    )
    assert fake_google.last.params == {}


async def test_move_task_with_every_option(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    other = fake_google.add_tasklist("Other")
    parent = fake_google.add_task(other["id"], "Parent")
    task = fake_google.add_task(DEFAULT_LIST_ID, "Mover")
    moved = await api.move_task(
        DEFAULT_LIST_ID,
        task["id"],
        parent=parent["id"],
        previous=parent["id"],
        destination_tasklist=other["id"],
    )
    assert moved.parent == parent["id"]
    assert fake_google.last.params == {
        "parent": parent["id"],
        "previous": parent["id"],
        "destinationTasklist": other["id"],
    }
    assert fake_google.task(other["id"], task["id"]) is not None
    assert fake_google.task(DEFAULT_LIST_ID, task["id"]) is None


async def test_clear_tasks(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    done = fake_google.add_task(DEFAULT_LIST_ID, "Done", status="completed")
    await api.clear_tasks("@default")
    assert fake_google.task(DEFAULT_LIST_ID, done["id"]) == {**done, "hidden": True}
    assert (fake_google.last.method, fake_google.last.path) == (
        "POST",
        "/tasks/v1/lists/@default/clear",
    )


# --- Path validation ---------------------------------------------------------


async def test_identifiers_are_percent_encoded(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.add_tasklist("Slashed", tasklist_id="a/b")
    fake_google.add_task("a/b", "Odd", task_id="t?x#y")
    task = await api.get_task("a/b", "t?x#y")
    assert task.title == "Odd"
    assert fake_google.last.path == "/tasks/v1/lists/a%2Fb/tasks/t%3Fx%23y"


Call = Callable[[GoogleTasksClient], Any]


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        (lambda api: api.get_tasklist(".."), "tasklist_id"),
        (lambda api: api.update_tasklist("", "t"), "tasklist_id"),
        (lambda api: api.patch_tasklist(".", "t"), "tasklist_id"),
        (lambda api: api.delete_tasklist(" "), "tasklist_id"),
        (lambda api: api.list_tasks("..", query=None), "tasklist_id"),
        (lambda api: api.get_task("@default", ".."), "task_id"),
        (lambda api: api.insert_task("\n", {"title": "x"}), "tasklist_id"),
        (lambda api: api.update_task("@default", "", {}), "task_id"),
        (lambda api: api.patch_task("@default", ".", {}), "task_id"),
        (lambda api: api.delete_task("..", "t"), "tasklist_id"),
        (lambda api: api.move_task("@default", "a\tb"), "task_id"),
        (lambda api: api.clear_tasks(".."), "tasklist_id"),
    ],
)
async def test_bad_identifiers_never_reach_google(
    api: GoogleTasksClient, fake_google: FakeGoogle, call: Call, argument: str
) -> None:
    with pytest.raises(InvalidArgumentError, match=f"^{argument} must not"):
        await call(api)
    assert fake_google.requests == []


# --- Error mapping -----------------------------------------------------------


async def test_google_error_becomes_api_error(api: GoogleTasksClient) -> None:
    with pytest.raises(
        ApiError, match=r"^Google Tasks API error 404 \(NOT_FOUND\): Requested"
    ) as e:
        await api.get_task(DEFAULT_LIST_ID, "missing")
    assert (e.value.status_code, e.value.api_status) == (404, "NOT_FOUND")


async def test_non_json_error_body_becomes_api_error(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.fail_next(500, "oops")
    with pytest.raises(ApiError, match=r"^Google Tasks API error 500: oops$") as e:
        await api.list_tasklists()
    assert e.value.api_status is None


@pytest.mark.parametrize(
    "exception",
    [
        lambda r: httpx.ConnectError("Connection refused", request=r),
        lambda r: httpx.ReadTimeout("timed out", request=r),
    ],
)
async def test_transport_failure_becomes_connectivity_error(
    api: GoogleTasksClient,
    fake_google: FakeGoogle,
    exception: Callable[[httpx.Request], Exception],
) -> None:
    fake_google.raise_next(exception)
    with pytest.raises(
        ConnectivityError, match=r"^Could not reach Google \((ConnectError|ReadTimeout)\)"
    ):
        await api.get_tasklist("@default")


@pytest.mark.parametrize(
    ("body", "model"),
    [
        ("not json", "TaskList"),
        ({"id": ["not", "a", "string"]}, "TaskList"),
    ],
)
async def test_unexpected_success_body_becomes_api_error(
    api: GoogleTasksClient, fake_google: FakeGoogle, body: dict[str, Any] | str, model: str
) -> None:
    fake_google.fail_next(200, body)
    with pytest.raises(ApiError, match=f"Unexpected response body from Google \\({model}"):
        await api.get_tasklist("@default")


# --- 401 refresh and retry ---------------------------------------------------


async def test_401_refreshes_once_and_retries(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.expire_access_tokens()
    tasklist = await api.get_tasklist("@default")
    assert tasklist.id == DEFAULT_LIST_ID
    assert len(fake_google.token_requests) == 1
    assert [r.authorization for r in fake_google.api_requests] == [
        f"Bearer {ACCESS_TOKEN}",
        "Bearer refreshed-token-1",
    ]
    await api.get_tasklist("@default")
    assert len(fake_google.token_requests) == 1
    assert fake_google.last.authorization == "Bearer refreshed-token-1"


async def test_401_without_refresh_credentials_is_an_api_error(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    api = GoogleTasksClient(http_client, _credentials(http_client, refresh=False))
    fake_google.expire_access_tokens()
    with pytest.raises(ApiError, match=r"^Google Tasks API error 401 \(UNAUTHENTICATED\)") as e:
        await api.list_tasklists()
    assert e.value.status_code == 401
    assert fake_google.token_requests == []
    assert len(fake_google.api_requests) == 1


async def test_second_401_after_refresh_is_not_retried_again(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.fail_next(401, match="/lists", times=2)
    with pytest.raises(ApiError, match="error 401"):
        await api.list_tasklists()
    assert len(fake_google.token_requests) == 1
    assert len(fake_google.api_requests) == 2


async def test_custom_base_url_is_used(
    http_client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    api = GoogleTasksClient(
        http_client, _credentials(http_client), base_url=f"{GOOGLE_TASKS_API_BASE}/"
    )
    await api.get_tasklist("@default")
    assert fake_google.last.path == f"{LISTS}/@default"


# --- Userinfo ----------------------------------------------------------------


async def test_get_userinfo(api: GoogleTasksClient, fake_google: FakeGoogle) -> None:
    info = await api.get_userinfo()
    assert info == CurrentUserInfo(
        sub="1234567890", email="user@example.com", name="Test User", scopes=[]
    )
    assert (fake_google.last.host, fake_google.last.path) == (USERINFO_HOST, "/oauth2/v3/userinfo")


async def test_get_userinfo_stringifies_and_tolerates_missing_claims(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.userinfo = {"sub": 42}
    info = await api.get_userinfo()
    assert (info.sub, info.email, info.name, info.scopes) == ("42", None, None, [])


async def test_get_userinfo_refreshes_on_401(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    fake_google.expire_access_tokens()
    info = await api.get_userinfo()
    assert info.email == "user@example.com"
    assert fake_google.last.authorization == "Bearer refreshed-token-1"


@pytest.mark.parametrize("body", ["<html>", '["a", "list"]'])
async def test_get_userinfo_rejects_unexpected_bodies(
    api: GoogleTasksClient, fake_google: FakeGoogle, body: str
) -> None:
    fake_google.fail_next(200, body, match="/userinfo")
    with pytest.raises(ApiError, match="Unexpected response body from Google userinfo"):
        await api.get_userinfo()


# --- Pagination --------------------------------------------------------------


async def test_fetch_all_tasklists_reads_every_page(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    for n in range(PAGE_SIZE + 4):
        fake_google.add_tasklist(f"L{n}")
    result = await fetch_all_tasklists(api)
    assert len(result.items) == PAGE_SIZE + 5
    assert result.next_page_token is None
    assert [r.params for r in fake_google.api_requests] == [
        {"maxResults": str(PAGE_SIZE)},
        {"maxResults": str(PAGE_SIZE), "pageToken": str(PAGE_SIZE)},
    ]


async def test_fetch_all_tasklists_single_page(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    result = await fetch_all_tasklists(api)
    assert [tl.id for tl in result.items] == [DEFAULT_LIST_ID]
    assert len(fake_google.api_requests) == 1


async def test_fetch_all_tasklists_stops_at_page_cap(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    cap = PAGE_SIZE * MAX_PAGES
    for n in range(cap):  # plus the default list: one item past the cap
        fake_google.tasklists[f"l{n}"] = {"id": f"l{n}", "title": str(n)}
    result = await fetch_all_tasklists(api)
    assert len(result.items) == cap
    assert result.next_page_token == str(cap)
    assert len(fake_google.api_requests) == MAX_PAGES


def _seed_tasks(fake_google: FakeGoogle, count: int) -> None:
    fake_google.tasks[DEFAULT_LIST_ID] = [{"id": f"t{n}", "title": str(n)} for n in range(count)]


async def test_fetch_all_tasks_reads_every_page(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    _seed_tasks(fake_google, PAGE_SIZE * 2 + 1)
    result = await fetch_all_tasks(api, "@default")
    assert [t.id for t in result.items] == [f"t{n}" for n in range(PAGE_SIZE * 2 + 1)]
    assert result.next_page_token is None
    assert [r.params.get("pageToken") for r in fake_google.api_requests] == [
        None,
        str(PAGE_SIZE),
        str(PAGE_SIZE * 2),
    ]


async def test_fetch_all_tasks_stops_at_page_cap(
    api: GoogleTasksClient, fake_google: FakeGoogle
) -> None:
    cap = PAGE_SIZE * MAX_PAGES
    _seed_tasks(fake_google, cap + 1)
    result = await fetch_all_tasks(api, DEFAULT_LIST_ID)
    assert len(result.items) == cap
    assert result.next_page_token == str(cap)
    assert len(fake_google.api_requests) == MAX_PAGES
