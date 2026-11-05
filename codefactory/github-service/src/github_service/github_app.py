"""GitHub App authentication and read/update operations used by webhook intake."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlparse

from githubkit import GitHub
from githubkit._httpx import httpx
from githubkit.auth import AppAuthStrategy
from githubkit.cache import MemCacheStrategy
from githubkit.exception import GraphQLFailed, RequestError as GitHubRequestError

from github_service.settings import RuntimeSettings


def _set_api_version(request: httpx.Request) -> None:
    request.headers["X-GitHub-Api-Version"] = "2022-11-28"


class GitHubAppClient:
    def __init__(
        self,
        *,
        app_id: int,
        private_key_path: Path,
        api_url: str = "https://api.github.com",
        status_field_name: str = "Status",
        ready_option_name: str = "Ready",
        in_progress_option_name: str = "In Progress",
        done_option_name: str = "Done",
        transport: httpx.BaseTransport | None = None,
    ):
        if app_id <= 0:
            raise ValueError("GITHUB_APP_ID must be a positive integer")
        if not all(
            name.strip()
            for name in (
                status_field_name,
                ready_option_name,
                in_progress_option_name,
                done_option_name,
            )
        ):
            raise ValueError("GitHub Project status names must not be empty")
        self._app_id = app_id
        self._status_field_name = status_field_name
        self._ready_option_name = ready_option_name
        self._in_progress_option_name = in_progress_option_name
        self._done_option_name = done_option_name
        self._private_key = private_key_path.read_text(encoding="utf-8")
        self._api_url = api_url.rstrip("/")
        self._app_auth = AppAuthStrategy(app_id, self._private_key)
        self._github = GitHub(
            self._app_auth,
            base_url=self._api_url,
            timeout=30.0,
            transport=transport,
            auto_retry=False,
            http_cache=False,
            cache_strategy=MemCacheStrategy(),
            event_hooks={"request": [_set_api_version]},
        )

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> GitHubAppClient:
        required = {
            "GITHUB_APP_ID": settings.github_app_id,
            "GITHUB_PRIVATE_KEY_PATH": settings.github_private_key_path,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"GitHub App settings are missing: {', '.join(missing)}")
        if settings.github_app_id <= 0:
            raise ValueError("GITHUB_APP_ID must be a positive integer")
        return cls(
            app_id=settings.github_app_id,
            private_key_path=settings.github_private_key_path,
            api_url=settings.github_api_url,
            status_field_name=settings.project_status_field_name,
            ready_option_name=settings.project_ready_option_name,
            in_progress_option_name=settings.project_in_progress_option_name,
            done_option_name=settings.project_done_option_name,
        )

    def get_project_item(
        self,
        item_node_id: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        result = self._graphql(
            """
            query($itemId: ID!) {
              node(id: $itemId) {
                ... on ProjectV2Item {
                  id
                                    isArchived
                                    project {
                                        id
                                        owner {
                                            __typename
                                            ... on Organization { login }
                                            ... on User { login }
                                        }
                                        fields(first: 100) {
                                            nodes {
                                                ... on ProjectV2SingleSelectField {
                                                    id
                                                    name
                                                    options { id name }
                                                }
                                            }
                                            pageInfo { hasNextPage }
                                        }
                                    }
                  content {
                    ... on Issue {
                      number
                                            state
                      title
                      body
                      repository { nameWithOwner }
                    }
                  }
                                    fieldValues(first: 100) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        optionId
                                                field { ... on ProjectV2SingleSelectField { id name } }
                      }
                    }
                                        pageInfo { hasNextPage }
                  }
                }
              }
            }
            """,
            {"itemId": item_node_id},
            installation_id=installation_id,
        )
        item = result.get("node")
        if not isinstance(item, Mapping):
            raise TypeError("Project item was not found or is not a Project v2 item")
        project = item.get("project")
        content = item.get("content")
        if not isinstance(project, Mapping) or not isinstance(content, Mapping):
            raise TypeError("Project item must contain a project and issue")
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise TypeError("Project item is missing its Project node ID")
        if item.get("id") != item_node_id:
            raise ValueError("GitHub returned a different Project item")
        project_owner = project.get("owner")
        owner_login = (
            project_owner.get("login") if isinstance(project_owner, Mapping) else None
        )
        owner_type = (
            project_owner.get("__typename")
            if isinstance(project_owner, Mapping)
            else None
        )
        owner_types = {"Organization": "organization", "User": "user"}
        if (
            not isinstance(owner_login, str)
            or not owner_login.strip()
            or owner_type not in owner_types
        ):
            raise TypeError("Project owner is unavailable or unsupported")
        (
            status_field_id,
            ready_option_id,
            in_progress_option_id,
            done_option_id,
        ) = self._project_status_configuration(project)
        repository = content.get("repository")
        field_values = item.get("fieldValues")
        page_info = (
            field_values.get("pageInfo") if isinstance(field_values, Mapping) else None
        )
        if (
            not isinstance(page_info, Mapping)
            or page_info.get("hasNextPage") is not False
        ):
            raise RuntimeError("GitHub Project item field values are incomplete")
        values = (
            field_values.get("nodes") if isinstance(field_values, Mapping) else None
        )
        status_option_id = None
        status_option_name = None
        status_value_count = 0
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                field = value.get("field")
                if isinstance(field, Mapping) and field.get("id") == status_field_id:
                    status_value_count += 1
                    status_option_id = value.get("optionId")
                    status_option_name = value.get("name")
        if status_value_count > 1:
            raise RuntimeError("GitHub Project item has ambiguous status values")
        if not isinstance(repository, Mapping):
            raise TypeError("Project item issue is missing its repository")
        repository_name = repository.get("nameWithOwner")
        if not isinstance(repository_name, str) or not repository_name.strip():
            raise TypeError("Project item issue is missing its repository")
        repository_info = self.get_repository(repository_name, installation_id)
        accessible_repository = repository_info.get("full_name")
        if (
            not isinstance(accessible_repository, str)
            or accessible_repository.casefold() != repository_name.casefold()
        ):
            raise ValueError(
                "Project item issue repository is not accessible to this installation"
            )
        return {
            "project_node_id": project_id,
            "project_owner": owner_login,
            "project_owner_type": owner_types[owner_type],
            "status_field_id": status_field_id,
            "status_option_id": status_option_id,
            "ready_option_id": ready_option_id,
            "in_progress_option_id": in_progress_option_id,
            "done_option_id": done_option_id,
            "status_field_name": self._status_field_name,
            "status_option_name": status_option_name,
            "is_archived": item.get("isArchived"),
            "done_option_name": self._done_option_name,
            "issue": {
                "number": content.get("number"),
                "state": content.get("state"),
                "title": content.get("title"),
                "body": content.get("body"),
                "repository": repository_name,
            },
        }

    def get_pull_request_ci(
        self,
        repository: str,
        pull_request_number: int,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            pull_request = client.rest.pulls.get(
                owner, repo, pull_request_number
            ).json()
            if not isinstance(pull_request, Mapping):
                raise TypeError("GitHub returned an invalid pull request")
            head = pull_request.get("head")
            if not isinstance(head, Mapping):
                raise TypeError("GitHub pull request is missing head information")
            head_sha = head.get("sha")
            pull_request_id = pull_request.get("id")
            if not isinstance(head_sha, str) or not isinstance(pull_request_id, int):
                raise TypeError("GitHub pull request is missing its id or head SHA")
            checks = list(
                client.rest.paginate(
                    client.rest.checks.list_for_ref,
                    map_func=self._pagination_mapper("check_runs"),
                    owner=owner,
                    repo=repo,
                    ref=head_sha,
                    per_page=100,
                )
            )
            statuses = list(
                client.rest.paginate(
                    client.rest.repos.list_commit_statuses_for_ref,
                    map_func=self._pagination_mapper(None),
                    owner=owner,
                    repo=repo,
                    ref=head_sha,
                    per_page=100,
                )
            )

        latest_check_runs = _latest_by(
            checks,
            key=lambda check: (check.get("name"), _app_id(check.get("app"))),
            timestamp_key="completed_at",
        )
        latest_statuses = _latest_by(
            statuses,
            key=lambda status: status.get("context"),
            timestamp_key="updated_at",
        )
        normalized_checks = [
            {
                "name": check.get("name", "unknown"),
                "status": (
                    "success"
                    if check.get("status") == "completed"
                    and check.get("conclusion") == "success"
                    else "failure"
                    if check.get("status") == "completed"
                    else "pending"
                ),
            }
            for check in latest_check_runs
            if isinstance(check, Mapping)
        ]
        normalized_checks.extend(
            {
                "name": status.get("context", "unknown"),
                "status": (
                    status.get("state")
                    if status.get("state") in {"success", "pending"}
                    else "failure"
                ),
            }
            for status in latest_statuses
            if isinstance(status, Mapping)
        )
        return {
            "pull_request_id": str(pull_request_id),
            "head_sha": head_sha,
            "ci_checks": normalized_checks,
            "ci_complete": all(
                check["status"] == "success" for check in normalized_checks
            ),
        }

    def create_pull_request(
        self,
        *,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.pulls.create(
                owner,
                repo,
                data={"title": title, "body": body, "head": head, "base": base},
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid pull request")
        pull_request_id = result.get("id")
        number = result.get("number")
        if not isinstance(pull_request_id, int) or not isinstance(number, int):
            raise TypeError("GitHub pull request response is missing its identity")
        return _pull_request_summary(
            result,
            repository=repository,
        )

    def update_pull_request_body(
        self,
        repository: str,
        number: int,
        body: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.pulls.update(
                owner,
                repo,
                number,
                data={"body": body},
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid pull request")
        return _pull_request_summary(result, repository=repository)

    def get_repository(
        self,
        repository: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id, timeout=timeout) as client:
            result = client.rest.repos.get(owner, repo).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned invalid repository metadata")
        return result

    def get_ref(
        self,
        repository: str,
        branch: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object] | None:
        owner, repo = repository.split("/", 1)
        try:
            with self._rest_client(installation_id, timeout=timeout) as client:
                result = client.rest.git.get_ref(
                    owner, repo, f"heads/{quote(branch, safe='/')}"
                ).json()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git ref")
        return result

    def get_commit(
        self,
        repository: str,
        sha: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id, timeout=timeout) as client:
            result = client.rest.git.get_commit(owner, repo, sha).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git commit")
        return result

    def get_tree(
        self,
        repository: str,
        sha: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id, timeout=timeout) as client:
            result = client.rest.git.get_tree(owner, repo, sha, recursive="1").json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git tree")
        return result

    def get_blob(
        self,
        repository: str,
        sha: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id, timeout=timeout) as client:
            result = client.rest.git.get_blob(owner, repo, sha).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git blob")
        return result

    def create_blob(
        self,
        repository: str,
        content: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.git.create_blob(
                owner,
                repo,
                data={
                    "content": base64.b64encode(content.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "encoding": "base64",
                },
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git blob")
        return result

    def create_tree(
        self,
        repository: str,
        base_tree: str,
        entries: list[Mapping[str, object]],
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.git.create_tree(
                owner,
                repo,
                data={
                    "base_tree": base_tree,
                    "tree": [dict(entry) for entry in entries],
                },
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git tree")
        return result

    def create_commit(
        self,
        repository: str,
        message: str,
        tree: str,
        parent: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.git.create_commit(
                owner,
                repo,
                data={"message": message, "tree": tree, "parents": [parent]},
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid Git commit")
        return result

    def create_ref(
        self,
        repository: str,
        branch: str,
        sha: str,
        installation_id: int,
    ) -> None:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            client.rest.git.create_ref(
                owner,
                repo,
                data={"ref": f"refs/heads/{branch}", "sha": sha},
            )

    def update_ref(
        self,
        repository: str,
        branch: str,
        sha: str,
        installation_id: int,
    ) -> None:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            client.rest.git.update_ref(
                owner,
                repo,
                f"heads/{quote(branch, safe='/')}",
                data={"sha": sha, "force": False},
            )

    def get_pull_request(
        self,
        repository: str,
        number: int,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.pulls.get(owner, repo, number).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid pull request")
        return _pull_request_summary(result, repository=repository)

    def get_pull_request_review_comments(
        self,
        repository: str,
        pull_request_number: int,
        review_id: int,
        installation_id: int,
    ) -> list[Mapping[str, object]]:
        owner, repo = repository.split("/", 1)
        comments: list[Mapping[str, object]] = []
        with self._rest_client(installation_id) as client:
            for comment in client.rest.paginate(
                client.rest.pulls.list_comments_for_review,
                map_func=self._pagination_mapper(None),
                owner=owner,
                repo=repo,
                pull_number=pull_request_number,
                review_id=review_id,
                per_page=20,
            ):
                if not isinstance(comment, Mapping):
                    raise TypeError("GitHub returned an invalid review comment")
                comments.append(dict(comment))
                if len(comments) == 20:
                    break
        return comments

    def find_issue_comment(
        self,
        repository: str,
        issue_number: int,
        marker: str,
        installation_id: int,
    ) -> str | None:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            for comment in client.rest.paginate(
                client.rest.issues.list_comments,
                map_func=self._pagination_mapper(None),
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                per_page=100,
            ):
                if not isinstance(comment, Mapping):
                    raise TypeError("GitHub returned an invalid issue comment")
                body = comment.get("body")
                comment_id = comment.get("id")
                if isinstance(body, str) and marker in body:
                    if not isinstance(comment_id, (str, int)) or isinstance(
                        comment_id, bool
                    ):
                        raise TypeError("GitHub returned an invalid issue comment")
                    return str(comment_id)
        return None

    def create_issue_comment(
        self,
        repository: str,
        issue_number: int,
        body: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.issues.create_comment(
                owner,
                repo,
                issue_number,
                data={"body": body},
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid issue comment")
        return dict(result)

    def find_review_comment(
        self,
        repository: str,
        pull_request_number: int,
        marker: str,
        installation_id: int,
    ) -> str | None:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            for comment in client.rest.paginate(
                client.rest.pulls.list_review_comments,
                map_func=self._pagination_mapper(None),
                owner=owner,
                repo=repo,
                pull_number=pull_request_number,
                per_page=100,
            ):
                if not isinstance(comment, Mapping):
                    raise TypeError("GitHub returned an invalid review comment")
                body = comment.get("body")
                comment_id = comment.get("id")
                if isinstance(body, str) and marker in body:
                    if not isinstance(comment_id, (str, int)) or isinstance(
                        comment_id, bool
                    ):
                        raise TypeError("GitHub returned an invalid review comment")
                    return str(comment_id)
        return None

    def create_review_comment_reply(
        self,
        repository: str,
        pull_request_number: int,
        in_reply_to: int,
        body: str,
        installation_id: int,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            result = client.rest.pulls.create_reply_for_review_comment(
                owner,
                repo,
                pull_request_number,
                in_reply_to,
                data={"body": body},
            ).json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid review comment")
        return dict(result)

    def find_pull_requests(
        self,
        repository: str,
        branch: str,
        base: str,
        installation_id: int,
    ) -> list[Mapping[str, object]]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            results = list(
                client.rest.paginate(
                    client.rest.pulls.list,
                    map_func=self._pagination_mapper(None),
                    owner=owner,
                    repo=repo,
                    state="all",
                    head=f"{owner}:{branch}",
                    base=base,
                    per_page=100,
                )
            )
        summaries = []
        for result in results:
            if not isinstance(result, Mapping):
                raise TypeError("GitHub returned an invalid pull request")
            summaries.append(_pull_request_summary(result, repository=repository))
        return summaries

    def verify_pull_request_completion(
        self,
        repository: str,
        pull_request_number: int,
        expected_pull_request_id: str,
        installation_id: int,
        no_ci_resolution: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        owner, repo = repository.split("/", 1)
        with self._rest_client(installation_id) as client:
            pull_request = client.rest.pulls.get(
                owner, repo, pull_request_number
            ).json()
        if not isinstance(pull_request, Mapping):
            raise TypeError("GitHub returned an invalid pull request")
        pull_request_id = pull_request.get("id")
        merged = pull_request.get("merged") is True
        state = pull_request.get("state")
        if (
            str(pull_request_id) != expected_pull_request_id
            or not merged
            or state != "closed"
        ):
            return {"verified": False}

        ci = self.get_pull_request_ci(
            repository,
            pull_request_number,
            installation_id,
        )
        checks = ci.get("ci_checks")
        resolved_without_ci = (
            no_ci_resolution is not None
            and no_ci_resolution.get("decision") == "proceed_without_ci"
            and no_ci_resolution.get("pull_request_id") == expected_pull_request_id
        )
        verified = (
            str(ci.get("pull_request_id")) == expected_pull_request_id
            and ci.get("ci_complete") is True
            and isinstance(checks, list)
            and (
                (
                    bool(checks)
                    and all(
                        isinstance(check, Mapping) and check.get("status") == "success"
                        for check in checks
                    )
                )
                or (resolved_without_ci and not checks)
            )
        )
        return {"verified": verified}

    def mark_task_done(
        self,
        state: Mapping[str, object],
        installation_id: int,
    ) -> Mapping[str, object]:
        project_id = state.get("project_node_id")
        item_id = state.get("project_item_id")
        owner = state.get("project_owner")
        owner_type = state.get("project_owner_type")
        repository = state.get("target_repository")
        if (
            not isinstance(project_id, str)
            or not isinstance(item_id, str)
            or not isinstance(owner, str)
            or owner_type not in {"organization", "user"}
            or not isinstance(repository, str)
        ):
            raise TypeError("Task is missing its GitHub Project identity")
        resolved = self.get_project_item(item_id, installation_id)
        issue = resolved.get("issue")
        if (
            resolved.get("project_node_id") != project_id
            or resolved.get("project_owner") != owner
            or resolved.get("project_owner_type") != owner_type
            or not isinstance(issue, Mapping)
            or not isinstance(issue.get("repository"), str)
            or issue["repository"].casefold() != repository.casefold()
        ):
            raise ValueError("Task Project item no longer matches its saved identity")
        status_field_id = resolved.get("status_field_id")
        done_option_id = resolved.get("done_option_id")
        if not isinstance(status_field_id, str) or not isinstance(done_option_id, str):
            raise TypeError("Project status field or Done option is unavailable")
        result = self._graphql(
            """
            mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $projectId,
                itemId: $itemId,
                fieldId: $fieldId,
                value: { singleSelectOptionId: $optionId }
              }) {
                projectV2Item { id }
              }
            }
            """,
            {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": status_field_id,
                "optionId": done_option_id,
            },
            installation_id=installation_id,
        )
        updated_item = result.get("updateProjectV2ItemFieldValue")
        if not isinstance(updated_item, Mapping) or not isinstance(
            updated_item.get("projectV2Item"), Mapping
        ):
            raise TypeError("GitHub did not confirm the Project status update")
        return result

    def mark_task_in_progress(
        self,
        state: Mapping[str, object],
        installation_id: int,
    ) -> Mapping[str, object]:
        project_id = state.get("project_node_id")
        item_id = state.get("project_item_id")
        owner = state.get("project_owner")
        owner_type = state.get("project_owner_type")
        repository = state.get("target_repository")
        if (
            not isinstance(project_id, str)
            or not isinstance(item_id, str)
            or not isinstance(owner, str)
            or owner_type not in {"organization", "user"}
            or not isinstance(repository, str)
        ):
            raise TypeError("Task is missing its GitHub Project identity")
        resolved = self.get_project_item(item_id, installation_id)
        issue = resolved.get("issue")
        if (
            resolved.get("project_node_id") != project_id
            or resolved.get("project_owner") != owner
            or resolved.get("project_owner_type") != owner_type
            or not isinstance(issue, Mapping)
            or not isinstance(issue.get("repository"), str)
            or issue["repository"].casefold() != repository.casefold()
        ):
            raise ValueError("Task Project item no longer matches its saved identity")
        if resolved.get("is_archived") is True or issue.get("state") != "OPEN":
            raise ValueError("Task is no longer open and active in its Project")
        current_status = resolved.get("status_option_name")
        if current_status == self._in_progress_option_name:
            return {"updated": False, "status": current_status}
        if current_status != self._ready_option_name:
            raise ValueError("Task is no longer ready for execution")
        status_field_id = resolved.get("status_field_id")
        in_progress_option_id = resolved.get("in_progress_option_id")
        if not isinstance(status_field_id, str) or not isinstance(
            in_progress_option_id, str
        ):
            raise TypeError("Project In Progress status option is unavailable")
        result = self._graphql(
            """
            mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $projectId,
                itemId: $itemId,
                fieldId: $fieldId,
                value: { singleSelectOptionId: $optionId }
              }) {
                projectV2Item { id }
              }
            }
            """,
            {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": status_field_id,
                "optionId": in_progress_option_id,
            },
            installation_id=installation_id,
        )
        updated_item = result.get("updateProjectV2ItemFieldValue")
        if not isinstance(updated_item, Mapping) or not isinstance(
            updated_item.get("projectV2Item"), Mapping
        ):
            raise TypeError("GitHub did not confirm the Project status update")
        return result

    def close(self) -> None:
        self._github.__exit__()

    def _graphql(
        self,
        query: str,
        variables: Mapping[str, object],
        *,
        installation_id: int,
    ) -> Mapping[str, object]:
        try:
            client = self._sdk_client(installation_id)
            response = client.graphql.request(query, dict(variables))
        except GraphQLFailed as error:
            raise RuntimeError(
                f"GitHub GraphQL operation failed: {error.response.errors}"
            ) from error
        except GitHubRequestError as error:
            raise error.exc from error
        if not isinstance(response, Mapping):
            raise TypeError("GitHub GraphQL response is missing data")
        return response

    @contextmanager
    def _rest_client(
        self, installation_id: int, *, timeout: float | None = None
    ) -> Iterator[GitHub]:
        try:
            yield self._sdk_client(installation_id, timeout=timeout)
        except GitHubRequestError as error:
            raise error.exc from error

    def _pagination_mapper(self, collection_key: str | None):
        page_count = 0

        def map_page(response: Any) -> list[object]:
            nonlocal page_count
            page_count += 1
            next_link = response.raw_response.links.get("next")
            if next_link:
                origin = urlparse(self._api_url)
                next_origin = urlparse(next_link["url"])
                if (next_origin.scheme, next_origin.netloc) != (
                    origin.scheme,
                    origin.netloc,
                ):
                    raise ValueError("GitHub pagination link changed API host")
                if page_count == 1000:
                    raise RuntimeError(
                        "GitHub pagination exceeded the configured safety limit"
                    )
            payload = response.json()
            items = (
                payload
                if collection_key is None and isinstance(payload, list)
                else payload.get(collection_key)
                if collection_key is not None and isinstance(payload, Mapping)
                else None
            )
            if not isinstance(items, list):
                key = collection_key or "items"
                raise TypeError(f"GitHub response is missing {key}")
            return items

        return map_page

    def _sdk_client(self, installation_id: int, *, timeout: float | None = None):
        if isinstance(installation_id, bool) or not isinstance(installation_id, int):
            raise ValueError("installation_id must be a positive integer")
        if installation_id <= 0:
            raise ValueError("installation_id must be a positive integer")
        auth = self._app_auth.as_installation(installation_id)
        config = self._github.config
        if timeout is not None:
            config = replace(config, timeout=httpx.Timeout(timeout))
        return GitHub(auth, config=config)

    def _project_status_configuration(
        self,
        project: Mapping[str, object],
    ) -> tuple[str, str, str | None, str]:
        fields = project.get("fields")
        nodes = fields.get("nodes") if isinstance(fields, Mapping) else None
        page_info = fields.get("pageInfo") if isinstance(fields, Mapping) else None
        if (
            not isinstance(nodes, list)
            or not isinstance(page_info, Mapping)
            or page_info.get("hasNextPage") is not False
        ):
            raise RuntimeError("GitHub Project fields are incomplete")
        matches = [
            field
            for field in nodes
            if isinstance(field, Mapping)
            and field.get("name") == self._status_field_name
        ]
        if len(matches) != 1:
            raise RuntimeError("GitHub Project status field is missing or ambiguous")
        field_id = matches[0].get("id")
        options = matches[0].get("options")
        if (
            not isinstance(field_id, str)
            or not field_id
            or not isinstance(options, list)
        ):
            raise RuntimeError("GitHub Project status field is incomplete")

        def option_id(name: str) -> str:
            matching_options = [
                option
                for option in options
                if isinstance(option, Mapping) and option.get("name") == name
            ]
            if len(matching_options) != 1:
                raise RuntimeError(
                    f"GitHub Project option {name!r} is missing or ambiguous"
                )
            result = matching_options[0].get("id")
            if not isinstance(result, str) or not result:
                raise RuntimeError("GitHub Project status option is incomplete")
            return result

        def optional_option_id(name: str) -> str | None:
            if not any(
                isinstance(option, Mapping) and option.get("name") == name
                for option in options
            ):
                return None
            return option_id(name)

        return (
            field_id,
            option_id(self._ready_option_name),
            optional_option_id(self._in_progress_option_name),
            option_id(self._done_option_name),
        )


def _app_id(app: object) -> object:
    return app.get("id") if isinstance(app, Mapping) else None


def _pull_request_summary(
    result: Mapping[str, object],
    *,
    repository: str,
) -> Mapping[str, object]:
    pull_request_id = result.get("id")
    number = result.get("number")
    head = result.get("head")
    base = result.get("base")
    repository_info = result.get("repository")
    head_repository = head.get("repo") if isinstance(head, Mapping) else None
    base_repository = base.get("repo") if isinstance(base, Mapping) else None
    actual_repository = (
        repository_info.get("full_name")
        if isinstance(repository_info, Mapping)
        else base_repository.get("full_name")
        if isinstance(base_repository, Mapping)
        else None
    )
    actual_head_repository = (
        head_repository.get("full_name")
        if isinstance(head_repository, Mapping)
        else None
    )
    if (
        not isinstance(pull_request_id, int)
        or not isinstance(number, int)
        or not isinstance(actual_repository, str)
        or not isinstance(actual_head_repository, str)
        or not isinstance(head, Mapping)
        or not isinstance(base, Mapping)
        or not isinstance(head.get("ref"), str)
        or not isinstance(head.get("sha"), str)
        or not head.get("sha")
        or not isinstance(base.get("ref"), str)
        or not isinstance(base_repository, Mapping)
        or not isinstance(base_repository.get("full_name"), str)
    ):
        raise TypeError("GitHub pull request response is missing its identity or refs")
    if actual_repository.casefold() != repository.casefold():
        raise TypeError("GitHub pull request response repository did not match")
    if base_repository["full_name"].casefold() != repository.casefold():
        raise TypeError("GitHub pull request base repository did not match")
    return {
        "pull_request_id": str(pull_request_id),
        "repository": actual_repository,
        "head_repository": actual_head_repository,
        "head_sha": head["sha"],
        "base_sha": base.get("sha"),
        "body": result.get("body"),
        "number": number,
        "head": head["ref"],
        "base": base["ref"],
        "state": result.get("state"),
        "url": result.get("html_url"),
    }


def _latest_by(
    entries: list[object],
    *,
    key: Any,
    timestamp_key: str,
) -> list[Mapping[str, object]]:
    latest: dict[object, tuple[datetime, Mapping[str, object]]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        entry_key = key(entry)
        timestamp = (
            entry.get(timestamp_key)
            or entry.get("started_at")
            or entry.get("created_at")
            or entry.get("updated_at")
        )
        if not isinstance(timestamp, str):
            latest[(entry_key, "unknown-timestamp", entry.get("id", index))] = (
                datetime.min.replace(tzinfo=UTC),
                entry,
            )
            continue
        try:
            created_at = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        previous = latest.get(entry_key)
        if previous is None or created_at > previous[0]:
            latest[entry_key] = (created_at, entry)
    return [entry for _, entry in latest.values()]
