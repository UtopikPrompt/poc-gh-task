"""Authenticated client for GitHub-owned queue and bounded API capabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

import httpx


class ProposedFileChange(TypedDict):
    operation: Literal["create", "update"]
    path: str
    content: str


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

    def propose_changes(
        self,
        *,
        task_id: str,
        changes: Sequence[ProposedFileChange],
        summary: str = "",
        risk_summary: str = "",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": task_id,
            "changes": [dict(change) for change in changes],
        }
        if summary or risk_summary:
            if not summary or not risk_summary:
                raise ValueError(
                    "proposal summary and risk_summary must be provided together"
                )
            payload["summary"] = summary
            payload["risk_summary"] = risk_summary
        response = self._client.post(
            "/internal/github/propose-changes",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, Mapping):
            raise TypeError("GitHub service returned an invalid PR response")
        return dict(result)

    def candidate_source_paths(self, task_id: str) -> list[dict[str, object]]:
        response = self._client.post(
            "/internal/github/source-tree",
            json={"task_id": task_id},
        )
        response.raise_for_status()
        result = response.json()
        paths = result.get("paths") if isinstance(result, Mapping) else None
        if not isinstance(paths, list) or any(
            not isinstance(item, Mapping) for item in paths
        ):
            raise TypeError("GitHub service returned invalid source candidates")
        return [dict(item) for item in paths]

    def source_files(self, task_id: str, paths: Sequence[str]) -> list[dict[str, str]]:
        response = self._client.post(
            "/internal/github/source-files",
            json={"task_id": task_id, "paths": list(paths)},
        )
        response.raise_for_status()
        result = response.json()
        files = result.get("files") if isinstance(result, Mapping) else None
        if not isinstance(files, list) or any(
            not isinstance(item, Mapping) for item in files
        ):
            raise TypeError("GitHub service returned invalid source files")
        normalized: list[dict[str, str]] = []
        for item in files:
            path = item.get("path")
            content = item.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise TypeError("GitHub service returned invalid source file content")
            normalized.append({"path": path, "content": content})
        return normalized

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


def build_submit_proposal_operation(client: GitHubServiceClient):
    def submit(state: Mapping[str, object]) -> Mapping[str, object]:
        change = state.get("change")
        if not isinstance(change, Mapping):
            raise TypeError("change operation must return a file changes array")
        changes = change.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("change operation returned no file changes")
        typed_changes: list[ProposedFileChange] = []
        for item in changes:
            if not isinstance(item, Mapping) or set(item) != {
                "operation",
                "path",
                "content",
            }:
                raise ValueError("change operation returned an invalid file change")
            operation = item.get("operation")
            path = item.get("path")
            content = item.get("content")
            if (
                operation not in {"create", "update"}
                or not isinstance(path, str)
                or not isinstance(content, str)
            ):
                raise ValueError("change operation returned an invalid file change")
            typed_changes.append(
                {"operation": operation, "path": path, "content": content}
            )
        task_id = state.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task state is missing task_id")
        summary = change.get("summary", "")
        risk_summary = change.get("risk_summary", "")
        if (
            not isinstance(summary, str)
            or len(summary) > 1200
            or not isinstance(risk_summary, str)
            or len(risk_summary) > 1600
            or bool(summary.strip()) != bool(risk_summary.strip())
        ):
            raise ValueError("change operation returned invalid summary fields")
        return client.propose_changes(
            task_id=task_id,
            changes=typed_changes,
            summary=summary,
            risk_summary=risk_summary,
        )

    return submit
