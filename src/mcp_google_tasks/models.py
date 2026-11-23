"""Pydantic models for Google Tasks API v1 resources and tool results.

Field names are snake_case in Python and keep Google's camelCase as aliases;
``extra="allow"`` keeps any field Google adds later instead of dropping it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type TaskStatus = Literal["needsAction", "completed"]

_GOOGLE = ConfigDict(extra="allow", populate_by_name=True)


class TaskLink(BaseModel):
    """Link associated with a Google Task."""

    model_config = _GOOGLE

    type: str | None = None
    description: str | None = None
    link: str | None = None


class DriveResourceInfo(BaseModel):
    """Drive file metadata for tasks assigned from Google Docs."""

    model_config = _GOOGLE

    drive_file_id: str | None = Field(default=None, alias="driveFileId")
    resource_key: str | None = Field(default=None, alias="resourceKey")


class SpaceInfo(BaseModel):
    """Google Chat Space metadata for tasks assigned from Chat."""

    model_config = _GOOGLE

    space: str | None = None


class AssignmentInfo(BaseModel):
    """Origin metadata for an assigned Google Task."""

    model_config = _GOOGLE

    link_to_task: str | None = Field(default=None, alias="linkToTask")
    surface_type: str | None = Field(default=None, alias="surfaceType")
    drive_resource_info: DriveResourceInfo | None = Field(default=None, alias="driveResourceInfo")
    space_info: SpaceInfo | None = Field(default=None, alias="spaceInfo")


class Task(BaseModel):
    """Google Tasks API v1 Task resource."""

    model_config = _GOOGLE

    kind: str | None = None
    id: str | None = None
    etag: str | None = None
    title: str | None = None
    updated: str | None = None
    self_link: str | None = Field(default=None, alias="selfLink")
    parent: str | None = None
    position: str | None = None
    notes: str | None = None
    status: str | None = None
    due: str | None = None
    completed: str | None = None
    deleted: bool | None = None
    hidden: bool | None = None
    links: list[TaskLink] = Field(default_factory=list)
    web_view_link: str | None = Field(default=None, alias="webViewLink")
    assignment_info: AssignmentInfo | None = Field(default=None, alias="assignmentInfo")


class TasksResponse(BaseModel):
    """Paginated response from ``tasks.list``."""

    model_config = _GOOGLE

    kind: str | None = None
    etag: str | None = None
    next_page_token: str | None = Field(default=None, alias="nextPageToken")
    items: list[Task] = Field(default_factory=list)


class TaskList(BaseModel):
    """Google Tasks API v1 TaskList resource."""

    model_config = _GOOGLE

    kind: str | None = None
    id: str | None = None
    etag: str | None = None
    title: str | None = None
    updated: str | None = None
    self_link: str | None = Field(default=None, alias="selfLink")


class TaskListsResponse(BaseModel):
    """Paginated response from ``tasklists.list``."""

    model_config = _GOOGLE

    kind: str | None = None
    etag: str | None = None
    next_page_token: str | None = Field(default=None, alias="nextPageToken")
    items: list[TaskList] = Field(default_factory=list)


class OperationResult(BaseModel):
    """Standard confirmation payload for void API operations (delete/clear)."""

    status: Literal["ok"] = "ok"
    message: str


class CurrentUserInfo(BaseModel):
    """Authenticated Google user information extracted from OAuth claims."""

    sub: str | None = None
    email: str | None = None
    name: str | None = None
    scopes: list[str] = Field(default_factory=list)
