"""Authenticated client for GitHub-owned queue and bounded API capabilities."""

from __future__ import annotations

from collections.abc import Mapping

import httpx


class GitHubServiceClient:
    def __init__(
        self,
        *,
        service_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ):
        if not token:
            raise ValueError("internal service token must be configured")
        self._client = httpx.Client(
            base_url=service_url.rstrip("/"),
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def claim_event(self) -> dict[str, object] | None:
        response = self._client.post("/internal/events/claim")
        response.raise_for_status()
        result = response.json()
        event = result.get("event") if isinstance(result, Mapping) else None
        return dict(event) if isinstance(event, Mapping) else None

    def acknowledge_event(self, event_id: str, claim_token: str) -> None:
        response = self._client.post(
            f"/internal/events/{event_id}/ack",
            json={"claim_token": claim_token},
        )
        response.raise_for_status()

    def retry_event(self, event_id: str, claim_token: str, error: str) -> None:
        response = self._client.post(
            f"/internal/events/{event_id}/retry",
            json={"claim_token": claim_token, "error": error[:2000]},
        )
        response.raise_for_status()

    def publish_pr(
        self,
        *,
        task_id: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, object]:
        response = self._client.post(
            "/internal/github/publish-pr",
            json={
                "task_id": task_id,
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub service returned an invalid PR response")
        return dict(result)

    def complete_task(self, task_id: str) -> dict[str, object]:
        response = self._client.post(
            "/internal/github/complete-task",
            json={"task_id": task_id},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub service returned an invalid completion response")
        return dict(result)

    def close(self) -> None:
        self._client.close()


def build_publish_pr_operation(client: GitHubServiceClient):
    def publish(state: Mapping[str, object]) -> Mapping[str, object]:
        change = state.get("change")
        if not isinstance(change, Mapping):
            raise TypeError("change operation must return PR publication metadata")
        values = {
            "title": change.get("pull_request_title"),
            "body": change.get("pull_request_body"),
            "head": change.get("head_branch"),
            "base": change.get("base_branch"),
        }
        if any(
            not isinstance(value, str) or not value.strip() for value in values.values()
        ):
            raise ValueError("change operation returned incomplete PR metadata")
        task_id = state.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task state is missing task_id")
        return client.publish_pr(task_id=task_id, **values)

    return publish
