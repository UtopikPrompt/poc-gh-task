"""Task lifecycle rules, independent of workflow and infrastructure libraries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import re


@dataclass(frozen=True)
class Task:
    status: object
    target_repositories: object
    workflow_status: object = None
    waiting_for: object = None
    pull_request_id: object = None
    event_pull_request_id: object = None
    ci_checks: object = None
    ci_complete: object = False
    ci_status: object = None
    merged: object = False
    no_ci_resolution: object = None
    review_feedback: object = None
    review_comment_ids: object = None
    repair_direction: object = None
    pending_question: object = None
    pending_question_comment_id: object = None
    pending_question_source_comment_id: object = None
    human_feedback_reply: object = None
    reply_to_comment_id: object = None
    reply_comment_id: object = None
    reply_action: object = None
    last_human_feedback_reply_comment_id: object = None
    last_human_feedback_reply_to_comment_id: object = None
    clarification_base_review_feedback: object = None
    clarification_base_repair_direction: object = None
    head_sha: object = None
    event_head_sha: object = None

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> Task:
        return cls(
            status=state.get("task_status"),
            target_repositories=state.get("target_repositories", []),
            workflow_status=state.get("workflow_status"),
            waiting_for=state.get("waiting_for"),
            pull_request_id=state.get("pull_request_id"),
            event_pull_request_id=state.get("event_pull_request_id"),
            ci_checks=state.get("ci_checks"),
            ci_complete=state.get("ci_complete", False),
            ci_status=state.get("ci_status"),
            merged=state.get("merged", False),
            no_ci_resolution=state.get("no_ci_resolution"),
            review_feedback=state.get("review_feedback"),
            review_comment_ids=state.get("review_comment_ids"),
            repair_direction=state.get("repair_direction"),
            pending_question=state.get("pending_question"),
            pending_question_comment_id=state.get("pending_question_comment_id"),
            pending_question_source_comment_id=state.get(
                "pending_question_source_comment_id"
            ),
            human_feedback_reply=state.get("human_feedback_reply"),
            reply_to_comment_id=state.get("reply_to_comment_id"),
            reply_comment_id=state.get("reply_comment_id"),
            reply_action=state.get("reply_action"),
            last_human_feedback_reply_comment_id=state.get(
                "last_human_feedback_reply_comment_id"
            ),
            last_human_feedback_reply_to_comment_id=state.get(
                "last_human_feedback_reply_to_comment_id"
            ),
            clarification_base_review_feedback=state.get(
                "clarification_base_review_feedback"
            ),
            clarification_base_repair_direction=state.get(
                "clarification_base_repair_direction"
            ),
            head_sha=state.get("head_sha"),
            event_head_sha=state.get("event_head_sha"),
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
        if not isinstance(checks, list):
            return "unavailable"
        if not checks:
            return "no_checks" if self.ci_complete is True else "pending"

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
            self.workflow_status
            not in {
                "awaiting_ci",
                "awaiting_human_merge",
                "no_ci_checks",
            }
            or not self.pull_request_id
            or self.event_pull_request_id != self.pull_request_id
            or (
                isinstance(self.head_sha, str)
                and self.head_sha
                and self.event_head_sha != self.head_sha
            )
        ):
            return {"event_status": "ignored"}
        if self.no_ci_resolution is not None:
            return {"event_status": "ignored_after_no_ci_resolution"}

        decision = self.ci_decision()
        if decision == "failure":
            return {"workflow_status": "repair_needed", "ci_status": decision}
        if decision == "success":
            return {"workflow_status": "ci_succeeded", "ci_status": decision}
        if decision == "no_checks":
            return {
                "workflow_status": "no_ci_checks",
                "waiting_for": "operator_decision",
                "ci_status": "unavailable",
            }
        return {"workflow_status": "awaiting_ci", "ci_status": decision}

    def resolve_no_ci(
        self,
        *,
        actor: object,
        decided_at: object,
        decision: object,
    ) -> dict[str, object]:
        if (
            self.workflow_status != "no_ci_checks"
            or self.ci_status != "unavailable"
            or self.ci_decision() != "no_checks"
            or not isinstance(self.pull_request_id, str)
            or not self.pull_request_id
            or decision != "proceed_without_ci"
            or not isinstance(actor, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", actor)
            or not isinstance(decided_at, str)
            or not _is_aware_datetime(decided_at)
        ):
            return {"event_status": "ignored_no_ci_resolution"}
        return {
            "workflow_status": "awaiting_human_merge",
            "waiting_for": "human_merge",
            "ci_status": "unavailable",
            "no_ci_resolution": {
                "actor": actor,
                "decided_at": decided_at,
                "decision": decision,
                "pull_request_id": self.pull_request_id,
            },
            "event_status": "no_ci_resolved",
        }

    def record_changes_requested(self) -> dict[str, object]:
        feedback = self.review_feedback
        if (
            self.workflow_status
            not in {
                "awaiting_ci",
                "awaiting_human_merge",
                "no_ci_checks",
                "repair_unresolved",
            }
            or not isinstance(self.pull_request_id, str)
            or not self.pull_request_id
            or self.event_pull_request_id != self.pull_request_id
            or not isinstance(feedback, str)
            or not feedback.strip()
        ):
            return {"event_status": "ignored"}
        return {
            "workflow_status": "repair_needed",
            "waiting_for": "repair_changes",
            "ci_status": "pending",
            "ci_checks": [],
            "ci_complete": False,
            "no_ci_resolution": None,
            "review_feedback": feedback,
            "repair_direction": "",
            "pending_question": "",
            "pending_question_comment_id": "",
            "pending_question_source_comment_id": "",
            "human_feedback_reply": "",
            "last_human_feedback_reply_comment_id": "",
            "last_human_feedback_reply_to_comment_id": "",
            "clarification_base_review_feedback": "",
            "clarification_base_repair_direction": "",
            "event_status": "review_changes_requested",
        }

    def record_human_feedback_reply(self) -> dict[str, object]:
        reply = self.human_feedback_reply
        source_comment_id = self.pending_question_source_comment_id
        is_pending_answer = (
            self.workflow_status == "repair_unresolved"
            and self.waiting_for == "human_feedback"
        )
        is_revision = (
            self.reply_action == "edited"
            and self.workflow_status
            in {"repair_needed", "awaiting_ci", "awaiting_human_merge"}
            and self.reply_comment_id == self.last_human_feedback_reply_comment_id
            and self.reply_to_comment_id == self.last_human_feedback_reply_to_comment_id
        )
        if isinstance(reply, str):
            reply = re.sub(r"^/agent(?:[ \t]+|\r?\n+)", "", reply.strip()).strip()
        if (
            not (is_pending_answer or is_revision)
            or not isinstance(self.pending_question, str)
            or not self.pending_question.strip()
            or not isinstance(self.pull_request_id, str)
            or not self.pull_request_id
            or self.event_pull_request_id != self.pull_request_id
            or not isinstance(reply, str)
            or not reply.strip()
            or not isinstance(self.reply_comment_id, str)
            or not self.reply_comment_id.isdecimal()
            or int(self.reply_comment_id) <= 0
            or self.reply_action not in {"created", "edited"}
            or (self.reply_action == "created" and not is_pending_answer)
            or (is_revision and self.reply_action != "edited")
            or (
                source_comment_id
                and (
                    not isinstance(source_comment_id, str)
                    or not source_comment_id.isdecimal()
                    or int(source_comment_id) <= 0
                )
            )
            or (
                isinstance(self.pending_question_source_comment_id, str)
                and self.pending_question_source_comment_id
                and self.reply_to_comment_id != self.pending_question_comment_id
                and self.reply_to_comment_id != self.pending_question_source_comment_id
            )
            or (
                not self.pending_question_source_comment_id
                and self.reply_to_comment_id is not None
            )
        ):
            return {"event_status": "ignored"}
        base_feedback = (
            self.clarification_base_review_feedback
            if is_revision
            else self.review_feedback
        )
        base_direction = (
            self.clarification_base_repair_direction
            if is_revision
            else self.repair_direction
        )
        base_feedback = base_feedback if isinstance(base_feedback, str) else ""
        base_direction = base_direction if isinstance(base_direction, str) else ""
        return {
            "workflow_status": "repair_needed",
            "waiting_for": "repair_changes",
            "repair_source_comment_id": source_comment_id or "",
            "review_comment_ids": [source_comment_id] if source_comment_id else [],
            "repair_direction": (
                f"{base_direction.strip()}\n\nHuman clarification:\n{reply.strip()}"
                if base_direction.strip()
                else reply.strip()
            ),
            "review_feedback": (
                f"{base_feedback.strip()}\n\nHuman clarification:\n{reply.strip()}"
                if base_feedback.strip()
                else f"Human clarification:\n{reply.strip()}"
            ),
            "human_feedback_reply": reply.strip(),
            "last_human_feedback_reply_comment_id": self.reply_comment_id,
            "last_human_feedback_reply_to_comment_id": self.reply_to_comment_id,
            "clarification_base_review_feedback": base_feedback,
            "clarification_base_repair_direction": base_direction,
            "event_status": "human_feedback_reply",
        }

    def record_agent_repair_requested(self) -> dict[str, object]:
        direction = self.repair_direction
        head_sha = self.event_head_sha
        if (
            self.workflow_status
            not in {
                "awaiting_ci",
                "awaiting_human_merge",
                "no_ci_checks",
                "repair_unresolved",
            }
            or not isinstance(self.pull_request_id, str)
            or not self.pull_request_id
            or self.event_pull_request_id != self.pull_request_id
            or not isinstance(direction, str)
            or not direction.strip()
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            return {"event_status": "ignored"}
        return {
            "workflow_status": "repair_needed",
            "waiting_for": "repair_changes",
            "ci_status": "pending",
            "ci_checks": [],
            "ci_complete": False,
            "no_ci_resolution": None,
            "review_feedback": "",
            "repair_direction": direction.strip(),
            "pending_question": "",
            "pending_question_comment_id": "",
            "pending_question_source_comment_id": "",
            "human_feedback_reply": "",
            "last_human_feedback_reply_comment_id": "",
            "last_human_feedback_reply_to_comment_id": "",
            "clarification_base_review_feedback": "",
            "clarification_base_repair_direction": "",
            "event_status": "agent_repair_requested",
        }

    def can_complete(self) -> bool:
        shared_requirements = (
            self.workflow_status == "awaiting_human_merge"
            and self.pull_request_id is not None
            and self.event_pull_request_id == self.pull_request_id
            and self.merged is True
        )
        successful_ci = self.ci_status == "success" and self.ci_decision() == "success"
        resolution = self.no_ci_resolution
        resolved_without_ci = (
            self.ci_status == "unavailable"
            and self.ci_decision() == "no_checks"
            and isinstance(resolution, Mapping)
            and resolution.get("decision") == "proceed_without_ci"
            and set(resolution)
            == {"actor", "decided_at", "decision", "pull_request_id"}
            and resolution.get("pull_request_id") == self.pull_request_id
            and isinstance(resolution.get("actor"), str)
            and re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?",
                resolution["actor"],
            )
            and isinstance(resolution.get("decided_at"), str)
            and _is_aware_datetime(resolution["decided_at"])
        )
        return shared_requirements and (successful_ci or resolved_without_ci)


def _is_aware_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None
