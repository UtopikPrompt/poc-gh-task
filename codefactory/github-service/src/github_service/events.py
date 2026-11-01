"""Normalize GitHub lifecycle payloads into task graph events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class PullRequestTaskLookup(Protocol):
    def task_for_pull_request(
        self,
        repository: str,
        pull_request_number: int,
    ) -> str | None: ...

    def task_context(self, task_id: str) -> Mapping[str, str] | None: ...


class GitHubEventAPI(Protocol):
    def get_project_item(
        self,
        item_node_id: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def get_pull_request_ci(
        self,
        repository: str,
        pull_request_number: int,
        installation_id: int,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class GitHubWebhook:
    event_name: str
    delivery_id: str
    installation_id: int
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ProjectEventConfig:
    status_field_name: str = "Status"
    ready_option_name: str = "Ready"


def normalize_installation_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("GitHub webhook installation.id must be a positive integer")
    if isinstance(value, str):
        value = value.strip()
        if not value.isdecimal():
            raise ValueError(
                "GitHub webhook installation.id must be a positive integer"
            )
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("GitHub webhook installation.id must be a positive integer")
    return value


class GitHubEventNormalizer:
    def __init__(self, api: GitHubEventAPI, config: ProjectEventConfig):
        self._api = api
        self._config = config

    def __call__(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
    ) -> Mapping[str, object] | list[Mapping[str, object]] | None:
        installation_id = normalize_installation_id(webhook.installation_id)
        if webhook.event_name == "projects_v2_item":
            return self._project_item_event(webhook, store, installation_id)
        if webhook.event_name == "check_run":
            return self._check_run_event(webhook, store, installation_id)
        if webhook.event_name == "pull_request":
            return self._pull_request_event(webhook, store, installation_id)
        return None

    def _project_item_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        item = _mapping(payload.get("projects_v2_item"))
        if payload.get("action") != "edited":
            return None
        item_node_id = item.get("node_id")
        if not isinstance(item_node_id, str) or not item_node_id:
            raise ValueError("Project item webhook is missing projects_v2_item.node_id")

        existing_context = store.task_context(item_node_id)
        if existing_context is not None and existing_context.get(
            "installation_id"
        ) != str(installation_id):
            return None

        resolved = self._api.get_project_item(item_node_id, installation_id)
        if (
            resolved.get("is_target_project") is not True
            or resolved.get("status_field_name") != self._config.status_field_name
            or resolved.get("status_option_name") != self._config.ready_option_name
        ):
            return None

        project_node_id = resolved.get("project_node_id")
        if not isinstance(project_node_id, str) or not project_node_id:
            raise ValueError("Resolved Project item is missing its project node ID")

        issue = _mapping(resolved.get("issue"))
        repository = issue.get("repository")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("Project item issue is missing its target repository")
        return {
            "task_id": item_node_id,
            "task_status": "ready",
            "event_type": "ready",
            "target_repositories": [repository],
            "issue": dict(issue),
            "project_node_id": project_node_id,
            "project_item_id": item_node_id,
            "installation_id": installation_id,
        }

    def _check_run_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> list[Mapping[str, object]] | None:
        payload = webhook.payload
        check_run = _mapping(payload.get("check_run"))
        repository = _repository_name(payload)
        if payload.get("action") not in {"created", "completed"}:
            return None
        pull_requests = check_run.get("pull_requests")
        if not isinstance(pull_requests, list):
            return None

        events: list[Mapping[str, object]] = []
        for pull_request in pull_requests:
            pull_request = _mapping(pull_request)
            number = pull_request.get("number")
            if not isinstance(number, int):
                continue
            task_id = store.task_for_pull_request(repository, number)
            if task_id is None:
                continue
            context = store.task_context(task_id)
            if context is None or context.get("installation_id") != str(
                installation_id
            ):
                continue
            task_installation_id = normalize_installation_id(
                context.get("installation_id")
            )
            ci = self._api.get_pull_request_ci(
                repository,
                number,
                task_installation_id,
            )
            checks = ci.get("ci_checks")
            if not isinstance(checks, list):
                raise TypeError("GitHub check resolver must return ci_checks")
            events.append(
                {
                    "task_id": task_id,
                    "event_type": "ci",
                    "installation_id": task_installation_id,
                    "event_pull_request_id": str(ci.get("pull_request_id", "")),
                    "ci_checks": checks,
                    "ci_complete": ci.get("ci_complete") is True,
                }
            )
        return events or None

    def _pull_request_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        pull_request = _mapping(payload.get("pull_request"))
        if payload.get("action") != "closed":
            return None
        repository = _repository_name(payload)
        number = payload.get("number")
        pull_request_id = pull_request.get("id")
        if not isinstance(number, int):
            return None
        task_id = store.task_for_pull_request(repository, number)
        if task_id is None:
            return None
        context = store.task_context(task_id)
        if context is None or context.get("installation_id") != str(installation_id):
            return None
        if not isinstance(pull_request_id, (str, int)):
            raise TypeError("pull request webhook is missing pull_request.id")
        return {
            "task_id": task_id,
            "event_type": "merged",
            "installation_id": installation_id,
            "event_pull_request_id": str(pull_request_id),
            "merged": pull_request.get("merged") is True,
        }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _repository_name(payload: Mapping[str, object]) -> str:
    repository = _mapping(payload.get("repository"))
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not full_name.strip():
        raise ValueError("GitHub webhook is missing repository.full_name")
    return full_name
