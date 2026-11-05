"""GitHub Project reporting for accepted task execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class ExecutionStore(Protocol):
    def task_context(self, task_id: str) -> dict[str, str] | None: ...


class ExecutionGitHubAPI(Protocol):
    def mark_task_in_progress(
        self,
        state: Mapping[str, object],
        installation_id: int,
    ) -> Mapping[str, object]: ...


class GitHubTaskExecutionReporter:
    def __init__(self, *, store: ExecutionStore, github: ExecutionGitHubAPI):
        self._store = store
        self._github = github

    def __call__(self, task_id: str) -> dict[str, object]:
        context = self._store.task_context(task_id)
        if context is None:
            return {"started": False, "reason": "task_mapping_unavailable"}
        try:
            installation_id = int(context["installation_id"])
        except (KeyError, ValueError):
            return {"started": False, "reason": "task_mapping_invalid"}
        if (
            installation_id <= 0
            or not context.get("project_node_id")
            or not context.get("project_item_id")
            or not context.get("project_owner")
            or context.get("project_owner_type") not in {"organization", "user"}
            or not context.get("target_repository")
        ):
            return {"started": False, "reason": "task_mapping_invalid"}
        project_update = self._github.mark_task_in_progress(
            context,
            installation_id,
        )
        return {"started": True, "project_update": dict(project_update)}
