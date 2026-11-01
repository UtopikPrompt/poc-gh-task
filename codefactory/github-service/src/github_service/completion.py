"""GitHub-owned completion gate for a human-merged pull request."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class CompletionStore(Protocol):
    def pull_request_for_task(self, task_id: str) -> dict[str, object] | None: ...

    def task_context(self, task_id: str) -> dict[str, str] | None: ...


class CompletionGitHubAPI(Protocol):
    def verify_pull_request_completion(
        self,
        repository: str,
        pull_request_number: int,
        expected_pull_request_id: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def mark_task_done(
        self,
        state: Mapping[str, object],
        installation_id: int,
    ) -> Mapping[str, object]: ...


class GitHubTaskCompletion:
    def __init__(
        self,
        *,
        store: CompletionStore,
        github: CompletionGitHubAPI,
    ):
        self._store = store
        self._github = github

    def __call__(self, task_id: str) -> dict[str, object]:
        pull_request = self._store.pull_request_for_task(task_id)
        context = self._store.task_context(task_id)
        if pull_request is None or context is None:
            return {"completed": False, "reason": "task_mapping_unavailable"}
        repository = pull_request.get("repository")
        number = pull_request.get("number")
        pull_request_id = pull_request.get("pull_request_id")
        if (
            not isinstance(repository, str)
            or not isinstance(number, int)
            or not isinstance(pull_request_id, str)
            or not pull_request_id
            or not isinstance(context.get("target_repository"), str)
            or context["target_repository"].lower() != repository.lower()
            or not context.get("project_item_id")
            or not context.get("project_node_id")
        ):
            return {"completed": False, "reason": "task_mapping_invalid"}
        try:
            installation_id = int(context["installation_id"])
        except (KeyError, ValueError):
            return {"completed": False, "reason": "task_mapping_invalid"}
        if installation_id <= 0:
            return {"completed": False, "reason": "task_mapping_invalid"}

        verification = self._github.verify_pull_request_completion(
            repository,
            number,
            pull_request_id,
            installation_id,
        )
        if verification.get("verified") is not True:
            return {"completed": False, "reason": "merge_or_checks_unverified"}
        project_update = self._github.mark_task_done(
            {
                "project_node_id": context["project_node_id"],
                "project_item_id": context["project_item_id"],
            },
            installation_id,
        )
        return {"completed": True, "project_update": dict(project_update)}
