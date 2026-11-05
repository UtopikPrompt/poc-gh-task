"""GitHub-owned completion gate for a human-merged pull request."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import re
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
        no_ci_resolution: Mapping[str, object] | None = None,
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

    def __call__(
        self,
        task_id: str,
        no_ci_resolution: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
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
            or not context.get("project_owner")
            or context.get("project_owner_type") not in {"organization", "user"}
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

        if no_ci_resolution is not None and not _valid_no_ci_resolution(
            no_ci_resolution, pull_request_id
        ):
            return {"completed": False, "reason": "no_ci_resolution_invalid"}

        verification = self._github.verify_pull_request_completion(
            repository,
            number,
            pull_request_id,
            installation_id,
            no_ci_resolution,
        )
        if verification.get("verified") is not True:
            return {"completed": False, "reason": "merge_or_checks_unverified"}
        project_update = self._github.mark_task_done(
            {
                "project_node_id": context["project_node_id"],
                "project_item_id": context["project_item_id"],
                "project_owner": context["project_owner"],
                "project_owner_type": context["project_owner_type"],
                "target_repository": context["target_repository"],
            },
            installation_id,
        )
        return {"completed": True, "project_update": dict(project_update)}


def _valid_no_ci_resolution(
    resolution: Mapping[str, object],
    pull_request_id: str,
) -> bool:
    actor = resolution.get("actor")
    decided_at = resolution.get("decided_at")
    if (
        resolution.get("decision") != "proceed_without_ci"
        or set(resolution) != {"actor", "decided_at", "decision", "pull_request_id"}
        or resolution.get("pull_request_id") != pull_request_id
        or not isinstance(actor, str)
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", actor)
        or not isinstance(decided_at, str)
        or len(decided_at) > 40
    ):
        return False
    try:
        timestamp = datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None
