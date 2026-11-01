"""GitHub App authentication and read/update operations used by webhook intake."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from github_service.settings import RuntimeSettings


class GitHubAppClient:
    def __init__(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key_path: Path,
        api_url: str = "https://api.github.com",
        status_field_id: str,
        done_option_id: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key_path.read_text(encoding="utf-8")
        self._api_url = api_url.rstrip("/")
        self._status_field_id = status_field_id
        self._done_option_id = done_option_id
        self._client = httpx.Client(
            base_url=self._api_url,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=UTC)
        self._token_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> GitHubAppClient:
        required = {
            "GITHUB_APP_ID": settings.github_app_id,
            "GITHUB_INSTALLATION_ID": settings.github_installation_id,
            "GITHUB_PRIVATE_KEY_PATH": settings.github_private_key_path,
            "GITHUB_PROJECT_NODE_ID": settings.project_node_id,
            "GITHUB_PROJECT_STATUS_FIELD_ID": settings.project_status_field_id,
            "GITHUB_PROJECT_DONE_OPTION_ID": settings.project_done_option_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"GitHub App settings are missing: {', '.join(missing)}")
        return cls(
            app_id=settings.github_app_id,
            installation_id=settings.github_installation_id,
            private_key_path=settings.github_private_key_path,
            api_url=settings.github_api_url,
            status_field_id=settings.project_status_field_id,
            done_option_id=settings.project_done_option_id,
        )

    def get_project_item(self, item_node_id: str) -> Mapping[str, object]:
        result = self._graphql(
            """
            query($itemId: ID!) {
              node(id: $itemId) {
                ... on ProjectV2Item {
                  id
                  project { id }
                  content {
                    ... on Issue {
                      number
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
                        field { ... on ProjectV2SingleSelectField { id } }
                      }
                    }
                  }
                }
              }
            }
            """,
            {"itemId": item_node_id},
        )
        item = result.get("node")
        if not isinstance(item, Mapping):
            raise TypeError("Project item was not found or is not a Project v2 item")
        project = item.get("project")
        content = item.get("content")
        if not isinstance(project, Mapping) or not isinstance(content, Mapping):
            raise TypeError("Project item must contain a project and issue")
        repository = content.get("repository")
        field_values = item.get("fieldValues")
        values = (
            field_values.get("nodes") if isinstance(field_values, Mapping) else None
        )
        status_option_id = None
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                field = value.get("field")
                if (
                    isinstance(field, Mapping)
                    and field.get("id") == self._status_field_id
                ):
                    status_option_id = value.get("optionId")
                    break
        if not isinstance(repository, Mapping):
            raise TypeError("Project item issue is missing its repository")
        return {
            "project_node_id": project.get("id"),
            "status_field_id": self._status_field_id,
            "status_option_id": status_option_id,
            "issue": {
                "number": content.get("number"),
                "title": content.get("title"),
                "body": content.get("body"),
                "repository": repository.get("nameWithOwner"),
            },
        }

    def get_pull_request_ci(
        self,
        repository: str,
        pull_request_number: int,
    ) -> Mapping[str, object]:
        pull_request = self._request(
            "GET",
            f"/repos/{repository}/pulls/{pull_request_number}",
        )
        if not isinstance(pull_request, Mapping):
            raise TypeError("GitHub returned an invalid pull request")
        head = pull_request.get("head")
        if not isinstance(head, Mapping):
            raise TypeError("GitHub pull request is missing head information")
        head_sha = head.get("sha")
        pull_request_id = pull_request.get("id")
        if not isinstance(head_sha, str) or not isinstance(pull_request_id, int):
            raise TypeError("GitHub pull request is missing its id or head SHA")

        checks = self._list_pages(
            f"/repos/{repository}/commits/{head_sha}/check-runs",
            "check_runs",
            {"per_page": 100},
        )
        statuses = self._list_pages(
            f"/repos/{repository}/commits/{head_sha}/statuses",
            "statuses",
            {"per_page": 100},
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
            "ci_checks": normalized_checks,
            "ci_complete": bool(normalized_checks)
            and all(check["status"] == "success" for check in normalized_checks),
        }

    def create_pull_request(
        self,
        *,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> Mapping[str, object]:
        result = self._request(
            "POST",
            f"/repos/{repository}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if not isinstance(result, Mapping):
            raise TypeError("GitHub returned an invalid pull request")
        pull_request_id = result.get("id")
        number = result.get("number")
        if not isinstance(pull_request_id, int) or not isinstance(number, int):
            raise TypeError("GitHub pull request response is missing its identity")
        return {
            "pull_request_id": str(pull_request_id),
            "repository": repository.lower(),
            "number": number,
            "url": result.get("html_url"),
        }

    def verify_pull_request_completion(
        self,
        repository: str,
        pull_request_number: int,
        expected_pull_request_id: str,
    ) -> Mapping[str, object]:
        pull_request = self._request(
            "GET",
            f"/repos/{repository}/pulls/{pull_request_number}",
        )
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

        ci = self.get_pull_request_ci(repository, pull_request_number)
        checks = ci.get("ci_checks")
        verified = (
            str(ci.get("pull_request_id")) == expected_pull_request_id
            and ci.get("ci_complete") is True
            and isinstance(checks, list)
            and bool(checks)
            and all(
                isinstance(check, Mapping) and check.get("status") == "success"
                for check in checks
            )
        )
        return {"verified": verified}

    def mark_task_done(self, state: Mapping[str, object]) -> Mapping[str, object]:
        project_id = state.get("project_node_id")
        item_id = state.get("project_item_id")
        if not isinstance(project_id, str) or not isinstance(item_id, str):
            raise TypeError("Task is missing its GitHub Project or item node ID")
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
                "fieldId": self._status_field_id,
                "optionId": self._done_option_id,
            },
        )
        updated_item = result.get("updateProjectV2ItemFieldValue")
        if not isinstance(updated_item, Mapping) or not isinstance(
            updated_item.get("projectV2Item"), Mapping
        ):
            raise TypeError("GitHub did not confirm the Project status update")
        return result

    def close(self) -> None:
        self._client.close()

    def _graphql(
        self,
        query: str,
        variables: Mapping[str, object],
    ) -> Mapping[str, object]:
        response = self._request(
            "POST",
            "/graphql",
            json={"query": query, "variables": dict(variables)},
        )
        if not isinstance(response, Mapping):
            raise TypeError("GitHub GraphQL response is invalid")
        errors = response.get("errors")
        if errors:
            raise RuntimeError(f"GitHub GraphQL operation failed: {errors}")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise TypeError("GitHub GraphQL response is missing data")
        return data

    def _list_pages(
        self,
        path: str,
        collection_key: str,
        params: Mapping[str, object],
    ) -> list[object]:
        url: str | None = path
        page_params: Mapping[str, object] | None = params
        results: list[object] = []
        for _ in range(1000):
            response = self._request_response("GET", url, params=page_params)
            payload = response.json()
            items = (
                payload
                if isinstance(payload, list)
                else payload.get(collection_key)
                if isinstance(payload, Mapping)
                else None
            )
            if not isinstance(items, list):
                raise TypeError(f"GitHub response is missing {collection_key}")
            results.extend(items)
            next_link = response.links.get("next")
            url = next_link.get("url") if next_link else None
            if url is not None:
                origin = urlparse(self._api_url)
                next_origin = urlparse(url)
                if (next_origin.scheme, next_origin.netloc) != (
                    origin.scheme,
                    origin.netloc,
                ):
                    raise ValueError("GitHub pagination link changed API host")
            page_params = None
            if url is None:
                return results
        raise RuntimeError("GitHub pagination exceeded the configured safety limit")

    def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> object:
        response = self._request_response(method, path, **kwargs)
        return response.json()

    def _request_response(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        response = self._client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {self._installation_token()}"},
            **kwargs,
        )
        response.raise_for_status()
        return response

    def _installation_token(self) -> str:
        with self._token_lock:
            if self._token and datetime.now(UTC) < self._token_expires_at:
                return self._token
            now = int(time.time())
            app_jwt = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)},
                self._private_key,
                algorithm="RS256",
            )
            response = self._client.post(
                f"/app/installations/{self._installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise TypeError("GitHub installation token response is invalid")
            token = payload.get("token")
            expires_at = payload.get("expires_at")
            if not isinstance(token, str) or not isinstance(expires_at, str):
                raise TypeError("GitHub installation token response is invalid")
            self._token = token
            self._token_expires_at = datetime.fromisoformat(expires_at) - timedelta(
                seconds=60
            )
            return token


def _app_id(app: object) -> object:
    return app.get("id") if isinstance(app, Mapping) else None


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
