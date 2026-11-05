"""GitHub-owned PR conversation capability for clarification requests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from github_service.events import normalize_installation_id


class ConversationStore(Protocol):
    def pull_request_for_task(self, task_id: str) -> dict[str, object] | None: ...

    def task_context(self, task_id: str) -> dict[str, str] | None: ...


class ConversationGitHubAPI(Protocol):
    def find_issue_comment(
        self,
        repository: str,
        issue_number: int,
        marker: str,
        installation_id: int,
    ) -> str | None: ...

    def create_issue_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def find_review_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
        installation_id: int,
    ) -> str | None: ...

    def create_review_comment_reply(
        self,
        repository: str,
        pull_request_number: int,
        in_reply_to: int,
        body: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...


class GitHubTaskConversation:
    def __init__(
        self,
        *,
        store: ConversationStore,
        github: ConversationGitHubAPI,
    ):
        self._store = store
        self._github = github

    def request_human_feedback(
        self,
        task_id: str,
        question: str,
        dedupe_key: str,
        source_review_comment_id: int | None = None,
    ) -> dict[str, object]:
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(question, str)
            or not question.strip()
            or len(question.encode("utf-8")) > 4000
            or not isinstance(dedupe_key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", dedupe_key)
            or (
                source_review_comment_id is not None
                and (
                    not isinstance(source_review_comment_id, int)
                    or isinstance(source_review_comment_id, bool)
                    or source_review_comment_id <= 0
                )
            )
        ):
            raise ValueError("human feedback request is invalid")
        pull_request = self._store.pull_request_for_task(task_id)
        context = self._store.task_context(task_id)
        if pull_request is None or context is None:
            raise ValueError("task pull request mapping is unavailable")
        repository = pull_request.get("repository")
        number = pull_request.get("number")
        if (
            not isinstance(repository, str)
            or not repository.strip()
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or not isinstance(context.get("target_repository"), str)
            or context["target_repository"].casefold() != repository.casefold()
        ):
            raise ValueError("task pull request mapping is invalid")
        installation_id = normalize_installation_id(context.get("installation_id"))
        marker = f"<!-- codefactory-human-feedback:{dedupe_key} -->"
        find_comment = (
            self._github.find_review_comment
            if source_review_comment_id is not None
            else self._github.find_issue_comment
        )
        existing = find_comment(repository, number, marker, installation_id)
        if existing is not None:
            return {"comment_id": existing, "created": False}
        body = f"{question.strip()}\n\n{marker}"
        if source_review_comment_id is not None:
            comment = self._github.create_review_comment_reply(
                repository,
                number,
                source_review_comment_id,
                body,
                installation_id,
            )
        else:
            comment = self._github.create_issue_comment(
                repository,
                number,
                body,
                installation_id,
            )
        comment_id = comment.get("id")
        if not isinstance(comment_id, (str, int)) or isinstance(comment_id, bool):
            raise TypeError("GitHub returned an invalid comment")
        return {"comment_id": str(comment_id), "created": True}
