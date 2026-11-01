"""GitHub-owned, repository-bounded pull request publication capability."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Protocol


class PublishingStore(Protocol):
    def task_context(self, task_id: str) -> dict[str, str] | None: ...

    def pull_request_for_task(self, task_id: str) -> dict[str, object] | None: ...

    def publication_lock(self, task_id: str) -> AbstractContextManager[None]: ...

    def register_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        task_id: str,
        pull_request_id: str | None = None,
    ) -> None: ...


class PullRequestAPI(Protocol):
    def create_pull_request(
        self,
        *,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> Mapping[str, object]: ...


class GitHubTaskPublisher:
    def __init__(self, *, store: PublishingStore, github: PullRequestAPI):
        self._store = store
        self._github = github

    def publish(
        self,
        *,
        task_id: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, object]:
        with self._store.publication_lock(task_id):
            return self._publish_locked(
                task_id=task_id,
                title=title,
                body=body,
                head=head,
                base=base,
            )

    def _publish_locked(
        self,
        *,
        task_id: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, object]:
        context = self._store.task_context(task_id)
        if context is None:
            raise ValueError("task has no GitHub-owned repository context")
        existing = self._store.pull_request_for_task(task_id)
        if existing is not None:
            existing_repository = existing.get("repository")
            if (
                not isinstance(existing_repository, str)
                or existing_repository.lower() != context["target_repository"].lower()
            ):
                raise ValueError("registered pull request is outside task repository")
            if not isinstance(existing.get("pull_request_id"), str):
                raise ValueError(
                    "registered pull request is missing its GitHub identity"
                )
            return existing
        result = self._github.create_pull_request(
            repository=context["target_repository"],
            title=title,
            body=body,
            head=head,
            base=base,
        )
        repository = result.get("repository")
        number = result.get("number")
        pull_request_id = result.get("pull_request_id")
        if (
            not isinstance(repository, str)
            or repository.lower() != context["target_repository"].lower()
            or not isinstance(number, int)
            or not isinstance(pull_request_id, str)
            or not pull_request_id
        ):
            raise TypeError("GitHub did not return a valid task pull request")
        self._store.register_pull_request(
            repository,
            number,
            task_id,
            pull_request_id,
        )
        return dict(result)
