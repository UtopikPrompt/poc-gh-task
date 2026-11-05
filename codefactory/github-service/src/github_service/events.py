"""Normalize GitHub lifecycle payloads into task graph events."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)
MAX_INLINE_REVIEW_HUNK_BYTES = 16 * 1024
MAX_INLINE_REVIEW_CONTEXTS_BYTES = 32 * 1024


def _log_webhook_decision(webhook: GitHubWebhook, reason: str) -> None:
    event_name = webhook.event_name
    action = webhook.payload.get("action")
    logger.info(
        "GitHub webhook decision",
        extra={
            "github_event": (
                event_name
                if isinstance(event_name, str)
                and re.fullmatch(r"[A-Za-z0-9_]{1,64}", event_name)
                else "other"
            ),
            "github_action": (
                action
                if isinstance(action, str)
                and re.fullmatch(r"[A-Za-z0-9_]{1,32}", action)
                else "other"
            ),
            "reason": reason,
        },
    )


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

    def get_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def get_pull_request_review_comments(
        self,
        repository: str,
        pull_request_number: int,
        review_id: int,
        installation_id: int,
    ) -> list[Mapping[str, object]]: ...


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
            event = self._project_item_event(webhook, store, installation_id)
        elif webhook.event_name == "check_run":
            event = self._check_run_event(webhook, store, installation_id)
        elif webhook.event_name == "pull_request":
            event = self._pull_request_event(webhook, store, installation_id)
        elif webhook.event_name == "pull_request_review":
            event = self._pull_request_review_event(webhook, store, installation_id)
        elif webhook.event_name == "issue_comment":
            event = self._issue_comment_event(webhook, store, installation_id)
        elif webhook.event_name == "pull_request_review_comment":
            event = self._pull_request_review_comment_event(
                webhook, store, installation_id
            )
        else:
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        return event

    def _project_item_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        item = _mapping(payload.get("projects_v2_item"))
        if payload.get("action") != "edited":
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        item_node_id = item.get("node_id")
        if not isinstance(item_node_id, str) or not item_node_id:
            raise ValueError("Project item webhook is missing projects_v2_item.node_id")

        existing_context = store.task_context(item_node_id)
        if existing_context is not None:
            _log_webhook_decision(webhook, "duplicate")
            return None

        resolved = self._api.get_project_item(item_node_id, installation_id)
        if (
            resolved.get("status_field_name") != self._config.status_field_name
            or resolved.get("status_option_name") != self._config.ready_option_name
        ):
            _log_webhook_decision(webhook, "status_mismatch")
            return None

        project_node_id = resolved.get("project_node_id")
        project_owner = resolved.get("project_owner")
        project_owner_type = resolved.get("project_owner_type")
        if not isinstance(project_node_id, str) or not project_node_id:
            raise ValueError("Resolved Project item is missing its project node ID")
        if not isinstance(project_owner, str) or not project_owner.strip():
            raise ValueError("Resolved Project item is missing its owner")
        if project_owner_type not in {"organization", "user"}:
            raise ValueError("Resolved Project item has an unsupported owner type")

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
            "project_owner": project_owner,
            "project_owner_type": project_owner_type,
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
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        pull_requests = check_run.get("pull_requests")
        if not isinstance(pull_requests, list):
            _log_webhook_decision(webhook, "no_matching_task")
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
            head_sha = ci.get("head_sha")
            if (
                not isinstance(checks, list)
                or not isinstance(head_sha, str)
                or not head_sha
            ):
                raise TypeError("GitHub check resolver must return checks and head SHA")
            events.append(
                {
                    "task_id": task_id,
                    "event_type": "ci",
                    "installation_id": task_installation_id,
                    "event_pull_request_id": str(ci.get("pull_request_id", "")),
                    "event_head_sha": head_sha,
                    "ci_checks": checks,
                    "ci_complete": ci.get("ci_complete") is True,
                }
            )
        if not events:
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        return events

    def _pull_request_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        pull_request = _mapping(payload.get("pull_request"))
        if payload.get("action") != "closed":
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        repository = _repository_name(payload)
        number = payload.get("number")
        pull_request_id = pull_request.get("id")
        if not isinstance(number, int):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None
        task_id = store.task_for_pull_request(repository, number)
        if task_id is None:
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        context = store.task_context(task_id)
        if context is None or context.get("installation_id") != str(installation_id):
            _log_webhook_decision(webhook, "no_matching_task")
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

    def _pull_request_review_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        review = _mapping(payload.get("review"))
        if (
            payload.get("action") != "submitted"
            or review.get("state") != "changes_requested"
        ):
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None

        repository = _repository_name(payload)
        number = _mapping(payload.get("pull_request")).get("number")
        review_id = review.get("id")
        pull_request_id = _mapping(payload.get("pull_request")).get("id")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(review_id, int)
            or isinstance(review_id, bool)
            or not isinstance(pull_request_id, (str, int))
        ):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None

        task_id = store.task_for_pull_request(repository, number)
        if task_id is None:
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        context = store.task_context(task_id)
        if context is None or context.get("installation_id") != str(installation_id):
            _log_webhook_decision(webhook, "no_matching_task")
            return None

        feedback: list[str] = []
        review_comment_ids: list[str] = []
        inline_review_contexts: dict[str, dict[str, object]] = {}
        inline_context_bytes = 0
        review_body = review.get("body")
        if isinstance(review_body, str) and review_body.strip():
            feedback.append(f"Review summary:\n{review_body.strip()}")
        comments = self._api.get_pull_request_review_comments(
            repository,
            number,
            review_id,
            installation_id,
        )
        current_pull_request = self._api.get_pull_request(
            repository,
            number,
            installation_id,
        )
        current_pull_request_id = current_pull_request.get("pull_request_id")
        head_sha = current_pull_request.get("head_sha")
        if (
            str(current_pull_request_id) != str(pull_request_id)
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            raise TypeError("GitHub did not return the matching pull request head")
        if not isinstance(comments, list):
            raise TypeError("GitHub review resolver must return a comment list")
        for comment in comments[:20]:
            if not isinstance(comment, Mapping):
                continue
            body = comment.get("body")
            if not isinstance(body, str) or not body.strip():
                continue
            path = comment.get("path")
            line = comment.get("line", comment.get("original_line"))
            location = f" on {path}" if isinstance(path, str) and path else ""
            if isinstance(line, int) and not isinstance(line, bool):
                location += f":{line}"
            comment_id = comment.get("id")
            if (
                not isinstance(comment_id, (str, int))
                or isinstance(comment_id, bool)
                or not str(comment_id).isdecimal()
                or int(comment_id) <= 0
            ):
                raise TypeError("GitHub review comment is missing its ID")
            comment_label = f" [review comment ID: {comment_id}]"
            comment_id_text = str(comment_id)
            review_comment_ids.append(comment_id_text)
            feedback.append(f"Inline comment{comment_label}{location}:\n{body.strip()}")
            inline_context = _inline_review_context(comment)
            if inline_context is not None:
                context_bytes = sum(
                    len(value.encode("utf-8"))
                    for value in inline_context.values()
                    if isinstance(value, str)
                )
                if (
                    inline_context_bytes + context_bytes
                    <= MAX_INLINE_REVIEW_CONTEXTS_BYTES
                ):
                    inline_review_contexts[comment_id_text] = inline_context
                    inline_context_bytes += context_bytes

        review_feedback = "\n\n".join(feedback)
        if not review_feedback:
            review_feedback = "Reviewer requested changes but provided no details."
        if len(review_feedback.encode("utf-8")) > 32 * 1024:
            raise ValueError("GitHub review feedback exceeds its byte limit")

        result = {
            "task_id": task_id,
            "event_type": "review_changes_requested",
            "installation_id": installation_id,
            "event_pull_request_id": str(pull_request_id),
            "event_head_sha": head_sha,
            "review_feedback": review_feedback,
            "review_comment_ids": review_comment_ids,
        }
        if inline_review_contexts:
            result["inline_review_contexts"] = inline_review_contexts
        return result

    def _pull_request_review_comment_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        comment = _mapping(payload.get("comment"))
        sender = _mapping(payload.get("sender"))
        pull_request = _mapping(payload.get("pull_request"))
        action = payload.get("action")
        if action == "edited" and comment.get("in_reply_to_id") is None:
            return self._agent_repair_comment_event(
                webhook, store, installation_id, comment, sender, pull_request
            )
        if (
            action == "created"
            and comment.get("in_reply_to_id") is not None
            and _agent_command_direction(comment.get("body"))
        ):
            return self._agent_repair_comment_event(
                webhook, store, installation_id, comment, sender, pull_request
            )
        reply_to_id = comment.get("in_reply_to_id")
        body = comment.get("body")
        if (
            action not in ("created", "edited")
            or sender.get("type") != "User"
            or (
                isinstance(sender.get("login"), str)
                and sender["login"].endswith("[bot]")
            )
            or not isinstance(reply_to_id, int)
            or isinstance(reply_to_id, bool)
            or reply_to_id <= 0
            or not isinstance(body, str)
            or not body.strip()
        ):
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        number = pull_request.get("number")
        pull_request_id = pull_request.get("id")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(pull_request_id, (str, int))
            or isinstance(pull_request_id, bool)
        ):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None
        repository = _repository_name(payload)
        task_id = store.task_for_pull_request(repository, number)
        if task_id is None:
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        context = store.task_context(task_id)
        if context is None or context.get("installation_id") != str(installation_id):
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        if len(body.encode("utf-8")) > 32 * 1024:
            raise ValueError("GitHub review comment exceeds its byte limit")
        comment_id = comment.get("id")
        if (
            not isinstance(comment_id, (str, int))
            or isinstance(comment_id, bool)
            or not str(comment_id).isdecimal()
            or int(comment_id) <= 0
        ):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None
        return {
            "task_id": task_id,
            "event_type": "human_feedback_reply",
            "installation_id": installation_id,
            "event_pull_request_id": str(pull_request_id),
            "human_feedback_reply": body.strip(),
            "reply_to_comment_id": str(reply_to_id),
            "reply_comment_id": str(comment_id),
            "reply_action": action,
        }

    def _agent_repair_comment_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
        comment: Mapping[str, object],
        sender: Mapping[str, object],
        pull_request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        body = comment.get("body")
        direction = _agent_command_direction(body)
        if (
            sender.get("type") != "User"
            or (
                isinstance(sender.get("login"), str)
                and sender["login"].endswith("[bot]")
            )
            or not direction
        ):
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        number = pull_request.get("number")
        pull_request_id = pull_request.get("id")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(pull_request_id, (str, int))
            or isinstance(pull_request_id, bool)
        ):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None
        repository = _repository_name(webhook.payload)
        task_id = store.task_for_pull_request(repository, number)
        if task_id is None:
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        context = store.task_context(task_id)
        if context is None or context.get("installation_id") != str(installation_id):
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        if len(direction.encode("utf-8")) > 32 * 1024:
            raise ValueError("GitHub repair direction exceeds its byte limit")
        comment_id = comment.get("id")
        if (
            not isinstance(comment_id, (str, int))
            or isinstance(comment_id, bool)
            or not str(comment_id).isdecimal()
            or int(comment_id) <= 0
        ):
            raise TypeError("GitHub repair comment is missing its ID")
        source_comment_id = comment.get("in_reply_to_id", comment_id)
        if (
            not isinstance(source_comment_id, (str, int))
            or isinstance(source_comment_id, bool)
            or not str(source_comment_id).isdecimal()
            or int(source_comment_id) <= 0
        ):
            raise TypeError("GitHub repair comment is missing its source ID")
        current_pull_request = self._api.get_pull_request(
            repository,
            number,
            installation_id,
        )
        head_sha = current_pull_request.get("head_sha")
        if (
            str(current_pull_request.get("pull_request_id")) != str(pull_request_id)
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            raise TypeError("GitHub did not return the matching pull request head")
        inline_context = _inline_review_context(comment)
        if inline_context is None:
            review_id = comment.get("pull_request_review_id")
            if (
                isinstance(review_id, int)
                and not isinstance(review_id, bool)
                and review_id > 0
            ):
                for source_comment in self._api.get_pull_request_review_comments(
                    repository,
                    number,
                    review_id,
                    installation_id,
                ):
                    if str(source_comment.get("id")) == str(comment_id):
                        inline_context = _inline_review_context(source_comment)
                        break
        if inline_context is None:
            raise ValueError(
                "GitHub repair comment is missing inline selection context"
            )
        return {
            "task_id": task_id,
            "event_type": "agent_repair_requested",
            "installation_id": installation_id,
            "event_pull_request_id": str(pull_request_id),
            "event_head_sha": head_sha,
            "repair_direction": direction,
            "review_comment_ids": [str(source_comment_id)],
            "command_comment_id": str(comment_id),
            "inline_review_context": inline_context,
        }

    def _issue_comment_event(
        self,
        webhook: GitHubWebhook,
        store: PullRequestTaskLookup,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        payload = webhook.payload
        issue = _mapping(payload.get("issue"))
        comment = _mapping(payload.get("comment"))
        sender = _mapping(payload.get("sender"))
        if (
            payload.get("action") != "created"
            or not isinstance(issue.get("pull_request"), Mapping)
            or sender.get("type") != "User"
            or (
                isinstance(sender.get("login"), str)
                and sender["login"].endswith("[bot]")
            )
        ):
            _log_webhook_decision(webhook, "unsupported_event_or_action")
            return None
        number = issue.get("number")
        pull_request_id = issue.get("id")
        body = comment.get("body")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not isinstance(pull_request_id, (str, int))
            or isinstance(pull_request_id, bool)
            or not isinstance(body, str)
            or not body.strip()
        ):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None
        repository = _repository_name(payload)
        task_id = store.task_for_pull_request(repository, number)
        if task_id is None:
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        context = store.task_context(task_id)
        if context is None or context.get("installation_id") != str(installation_id):
            _log_webhook_decision(webhook, "no_matching_task")
            return None
        if len(body.encode("utf-8")) > 32 * 1024:
            raise ValueError("GitHub comment exceeds its byte limit")
        comment_id = comment.get("id")
        if (
            not isinstance(comment_id, (str, int))
            or isinstance(comment_id, bool)
            or not str(comment_id).isdecimal()
            or int(comment_id) <= 0
        ):
            _log_webhook_decision(webhook, "repository_validation_failure")
            return None
        return {
            "task_id": task_id,
            "event_type": "human_feedback_reply",
            "installation_id": installation_id,
            "event_pull_request_id": str(pull_request_id),
            "human_feedback_reply": body.strip(),
            "reply_comment_id": str(comment_id),
            "reply_action": "created",
        }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _agent_command_direction(body: object) -> str:
    if not isinstance(body, str):
        return ""
    command = re.match(r"/agent(?:[ \t]+|\r?\n+)(.*)", body.strip(), re.DOTALL)
    return command.group(1).strip() if command is not None else ""


def _inline_review_context(comment: Mapping[str, object]) -> dict[str, object] | None:
    path = comment.get("path")
    line = comment.get("line")
    if line is None:
        line = comment.get("original_line")
    diff_hunk = comment.get("diff_hunk")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 240
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not isinstance(line, int)
        or isinstance(line, bool)
        or line <= 0
        or not isinstance(diff_hunk, str)
        or not diff_hunk.strip()
        or "\0" in diff_hunk
        or len(diff_hunk.encode("utf-8")) > MAX_INLINE_REVIEW_HUNK_BYTES
    ):
        return None
    context: dict[str, object] = {
        "path": path,
        "line": line,
        "diff_hunk": diff_hunk,
    }
    start_line = comment.get("start_line")
    if (
        isinstance(start_line, int)
        and not isinstance(start_line, bool)
        and start_line > 0
    ):
        context["start_line"] = start_line
    side = comment.get("side")
    if side in {"LEFT", "RIGHT"}:
        context["side"] = side
    return context


def _repository_name(payload: Mapping[str, object]) -> str:
    repository = _mapping(payload.get("repository"))
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not full_name.strip():
        raise ValueError("GitHub webhook is missing repository.full_name")
    return full_name
