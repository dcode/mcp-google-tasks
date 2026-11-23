# Tools and resources

The server exposes 15 tools and 2 resources covering the Google Tasks REST API v1. Every tool acts
as the signed-in Google account and returns structured output (a pydantic model with a schema).

Conventions that apply throughout:

- **`@default`** as a `tasklist_id` means the user's primary list. Most tools default to it.
- **Timestamps** are RFC 3339 with a time zone, for example `2026-12-01T00:00:00Z` or
  `2026-12-01T09:00:00-05:00`. A value without a time zone, or that is not a timestamp, is
  rejected with a clear error before anything is sent to Google.
- **Due dates are dates.** Google stores only the date part of `due`; the time is discarded.
- **Pagination.** List tools return `next_page_token` when there are more results; pass it back
  as `page_token`.
- **Errors** reach the model as a tool error with a plain message: Google's status and message
  for API errors, or a description of the invalid input.
- **IDs** are checked before use: empty values, `.`, `..` and control characters are rejected.

## Hints and read-only mode

Every tool declares MCP tool annotations so hosts can ask before a change. In the tables below,
**Read** means `readOnlyHint`, **Destructive** means `destructiveHint` (the host should confirm),
and **Idempotent** (`idempotentHint`, declared on write tools only) means repeating the call
with the same arguments has no further effect. "Not set" means the tool does not declare the
hint; for a write tool, MCP hosts then assume it may be destructive, so they treat it the same
as "Yes". `openWorldHint` is true on every tool except `get_current_user`.

With `GOOGLE_TASKS_READONLY=true` the write tools are **not registered at all**: they do not
appear in the tool list and cannot be called. The read tools and both resources stay available.

| Tool | Read | Destructive | Idempotent | In read-only mode |
|---|---|---|---|---|
| `get_current_user` | Yes | n/a | n/a | Available |
| `list_tasklists` | Yes | n/a | n/a | Available |
| `get_tasklist` | Yes | n/a | n/a | Available |
| `create_tasklist` | No | Not set | No | Hidden |
| `update_tasklist` | No | Not set | Yes | Hidden |
| `patch_tasklist` | No | Not set | Yes | Hidden |
| `delete_tasklist` | No | Yes | No | Hidden |
| `list_tasks` | Yes | n/a | n/a | Available |
| `get_task` | Yes | n/a | n/a | Available |
| `create_task` | No | Not set | No | Hidden |
| `update_task` | No | Not set | Yes | Hidden |
| `patch_task` | No | Not set | Yes | Hidden |
| `delete_task` | No | Yes | No | Hidden |
| `move_task` | No | Not set | No | Hidden |
| `clear_completed_tasks` | No | Yes | No | Hidden |

There is no dry-run mode: write tools act immediately. Hosts gate them through these hints, and
read-only mode is the off switch. [DESIGN.md](DESIGN.md) records why.

## Identity

### `get_current_user`

Who the server is acting as: the account's ID (`sub`), `email`, `name` and the granted `scopes`.
Useful to confirm the right account is connected before making changes.

No parameters. In `token` mode it calls Google's userinfo endpoint (and fails with an error if
that call fails); `scopes` is empty there. In the multi-user modes it reads the verified token's
claims and scopes.

## Task lists

### `list_tasklists`

All of the user's task lists (`tasklists.list`).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_results` | integer 1 to 100 | Google's default (20) | Lists per page |
| `page_token` | string | | Token from a previous call |

### `get_tasklist`

One task list by ID (`tasklists.get`).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasklist_id` | string | `@default` | List ID |

### `create_tasklist`

Create a task list (`tasklists.insert`). Returns the new list with its ID.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `title` | string, 1 to 1024 characters | required | Title |

### `update_tasklist`

Replace a task list's metadata (`tasklists.update`, HTTP `PUT`).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasklist_id` | string | required | List ID, or `@default` |
| `title` | string, 1 to 1024 characters | required | New title |

### `patch_tasklist`

Change only the given fields of a task list (`tasklists.patch`, HTTP `PATCH`).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasklist_id` | string | required | List ID, or `@default` |
| `title` | string, 1 to 1024 characters | required | New title |

### `delete_tasklist`

Delete a task list **and every task in it** (`tasklists.delete`). Cannot be undone. Returns a
confirmation message.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasklist_id` | string | required | List ID |

## Tasks

### `list_tasks`

Tasks in a list, with Google's full filtering (`tasks.list`).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasklist_id` | string | `@default` | List ID |
| `completed_min`, `completed_max` | timestamp | | Bounds on the completion time |
| `due_min`, `due_max` | timestamp | | Bounds on the due date |
| `updated_min` | timestamp | | Only tasks modified at or after this time |
| `max_results` | integer 1 to 100 | Google's default (20) | Tasks per page |
| `page_token` | string | | Token from a previous call |
| `show_completed` | boolean | Google's default (true) | Include completed tasks |
| `show_hidden` | boolean | Google's default (false) | Include hidden tasks |
| `show_deleted` | boolean | Google's default (false) | Include deleted tasks |
| `show_assigned` | boolean | Google's default (false) | Include tasks assigned from Docs or Chat |

Tasks completed in Google's own web and mobile apps become **hidden**. To see them, pass
`show_hidden=true` as well as `show_completed=true`.

### `get_task`

One task by ID (`tasks.get`).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task_id` | string | required | Task ID |
| `tasklist_id` | string | `@default` | List ID |

### `create_task`

Create a task (`tasks.insert`). Returns the new task with its ID.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `title` | string, up to 1024 characters | required | Title |
| `tasklist_id` | string | `@default` | List ID |
| `notes` | string, up to 8192 characters | | Notes |
| `due` | timestamp | | Due date (date part only is kept) |
| `status` | `needsAction` or `completed` | Google's default (`needsAction`) | Status |
| `completed` | timestamp | | Completion time |
| `parent` | string | | Create as a subtask of this task |
| `previous` | string | | Place after this sibling task |

### `update_task`

Replace a task (`tasks.update`, HTTP `PUT`). Optional fields that are left out may be cleared by
Google, so prefer `patch_task` for partial changes.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task_id` | string | required | Task ID |
| `title` | string, up to 1024 characters | required | Title |
| `tasklist_id` | string | `@default` | List ID |
| `notes` | string, up to 8192 characters | | Notes |
| `due` | timestamp | | Due date |
| `status` | `needsAction` or `completed` | `needsAction` | Status |
| `completed` | timestamp | | Completion time |
| `deleted` | boolean | | Deleted flag |
| `hidden` | boolean | | Hidden flag |

### `patch_task`

Change only the given fields of a task (`tasks.patch`, HTTP `PATCH`). At least one field besides
the IDs is required. Setting `status` to `needsAction` without a `completed` value also clears the
completion time, which reopens a completed task.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task_id` | string | required | Task ID |
| `tasklist_id` | string | `@default` | List ID |
| `title` | string, up to 1024 characters | | Title |
| `notes` | string, up to 8192 characters | | Notes |
| `due` | timestamp | | Due date |
| `status` | `needsAction` or `completed` | | Status |
| `completed` | timestamp | | Completion time |
| `deleted` | boolean | | Deleted flag |
| `hidden` | boolean | | Hidden flag |

To complete a task: `patch_task(task_id=..., status="completed")`.

### `delete_task`

Delete a task (`tasks.delete`). Returns a confirmation message.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task_id` | string | required | Task ID |
| `tasklist_id` | string | `@default` | List ID |

### `move_task`

Reorder a task, make it a subtask or a top-level task, or move it to another list (`tasks.move`).
Returns the moved task.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `task_id` | string | required | Task ID |
| `tasklist_id` | string | `@default` | Source list ID |
| `parent` | string | | New parent task; omit to move to the top level |
| `previous` | string | | New previous sibling; omit to move to the first position |
| `destination_tasklist` | string | | Target list ID; omit to stay in the same list |

### `clear_completed_tasks`

Clear every completed task from a list (`tasks.clear`). Google marks them hidden, so they no
longer appear in default listings. Returns a confirmation message.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tasklist_id` | string | `@default` | List ID |

## Resources

Resources are read-only and available in every mode, including read-only mode. Each returns
JSON and fetches **all** pages (100 items per page, at most 50 pages).

| URI | Contents |
|---|---|
| `gtasks://lists` | All of the user's task lists, in the shape of `tasklists.list` |
| `gtasks://lists/{tasklist_id}/tasks` | Tasks in one list, in the shape of `tasks.list`, with Google's default filters (completed tasks included, hidden and deleted tasks not) |
