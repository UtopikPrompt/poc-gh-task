"""Task lifecycle rules, independent of workflow and infrastructure libraries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    status: object
    target_repositories: object
    workflow_status: object = None
    pull_request_id: object = None
    event_pull_request_id: object = None
    ci_checks: object = None
    ci_complete: object = False
    ci_status: object = None
    merged: object = False

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> Task:
        return cls(
            status=state.get("task_status"),
            target_repositories=state.get("target_repositories", []),
            workflow_status=state.get("workflow_status"),
            pull_request_id=state.get("pull_request_id"),
            event_pull_request_id=state.get("event_pull_request_id"),
            ci_checks=state.get("ci_checks"),
            ci_complete=state.get("ci_complete", False),
            ci_status=state.get("ci_status"),
            merged=state.get("merged", False),
        )

    def ready_repository(self) -> str | None:
        repositories = self.target_repositories
        if self.status != "ready":
            return None
        if (
            not isinstance(repositories, list)
            or len(repositories) != 1
            or not isinstance(repositories[0], str)
            or not repositories[0].strip()
        ):
            return None
        return repositories[0]

    def ci_decision(self) -> str:
        checks = self.ci_checks
        if not isinstance(checks, list) or not checks:
            return "unavailable"

        statuses: list[object] = []
        for check in checks:
            if not isinstance(check, Mapping):
                return "pending"
            status = check.get("status")
            if status in {"failure", "error"}:
                return "failure"
            statuses.append(status)

        if self.ci_complete is True and all(status == "success" for status in statuses):
            return "success"
        return "pending"

    def record_ci(self) -> dict[str, str]:
        if (
            self.workflow_status not in {"awaiting_ci", "awaiting_human_merge"}
            or not self.pull_request_id
            or self.event_pull_request_id != self.pull_request_id
        ):
            return {"event_status": "ignored"}

        decision = self.ci_decision()
        if decision == "failure":
            return {"workflow_status": "repair_needed", "ci_status": decision}
        if decision == "success":
            return {"workflow_status": "ci_succeeded", "ci_status": decision}
        return {"workflow_status": "awaiting_ci", "ci_status": decision}

    def can_complete(self) -> bool:
        return (
            self.workflow_status == "awaiting_human_merge"
            and self.ci_status == "success"
            and self.ci_decision() == "success"
            and self.pull_request_id is not None
            and self.event_pull_request_id == self.pull_request_id
            and self.merged is True
        )
