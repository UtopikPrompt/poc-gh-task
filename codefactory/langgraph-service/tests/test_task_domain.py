import pytest
from langgraph_service.workflow.task import Task


@pytest.mark.parametrize("repositories", [[], ["one/repo", "two/repo"]])
def test_ready_task_requires_exactly_one_target_repository(repositories):
    task = Task.from_state(
        {"task_status": "ready", "target_repositories": repositories}
    )

    assert task.ready_repository() is None


def test_ready_task_accepts_one_nonblank_target_repository():
    task = Task.from_state(
        {"task_status": "ready", "target_repositories": ["example/project"]}
    )

    assert task.ready_repository() == "example/project"


@pytest.mark.parametrize(
    ("checks", "complete", "expected"),
    [
        ([{"status": "success"}], True, "success"),
        ([{"status": "success"}], False, "pending"),
        ([{"status": "failure"}], True, "failure"),
        ([], False, "pending"),
        ([], True, "no_checks"),
    ],
)
def test_ci_decision(checks, complete, expected):
    task = Task.from_state({"ci_checks": checks, "ci_complete": complete})

    assert task.ci_decision() == expected


@pytest.mark.parametrize(
    ("event_pull_request_id", "ci_status", "workflow_status", "merged"),
    [
        ("pr-1", "success", "awaiting_human_merge", True),
        ("pr-other", "success", "awaiting_human_merge", True),
        ("pr-1", "pending", "awaiting_human_merge", True),
        ("pr-1", "success", "awaiting_ci", True),
        ("pr-1", "success", "awaiting_human_merge", False),
    ],
)
def test_completion_requires_successful_ci_matching_pr_and_human_merge(
    event_pull_request_id, ci_status, workflow_status, merged
):
    task = Task.from_state(
        {
            "workflow_status": workflow_status,
            "pull_request_id": "pr-1",
            "event_pull_request_id": event_pull_request_id,
            "ci_checks": [{"status": "success"}],
            "ci_complete": True,
            "ci_status": ci_status,
            "merged": merged,
        }
    )

    assert task.can_complete() is (
        event_pull_request_id == "pr-1"
        and ci_status == "success"
        and workflow_status == "awaiting_human_merge"
        and merged
    )


@pytest.mark.parametrize(
    ("event_pull_request_id", "checks", "complete", "expected"),
    [
        ("pr-1", [{"status": "success"}], True, "ci_succeeded"),
        ("pr-1", [{"status": "failure"}], True, "repair_needed"),
        ("pr-1", [{"status": "pending"}], False, "awaiting_ci"),
        ("pr-other", [{"status": "success"}], True, "ignored"),
    ],
)
def test_ci_event_applies_domain_transition(
    event_pull_request_id, checks, complete, expected
):
    task = Task.from_state(
        {
            "workflow_status": "awaiting_ci",
            "pull_request_id": "pr-1",
            "event_pull_request_id": event_pull_request_id,
            "ci_checks": checks,
            "ci_complete": complete,
        }
    )

    transition = task.record_ci()

    assert transition.get("workflow_status", transition.get("event_status")) == expected


def test_empty_completed_ci_is_an_explicit_operator_decision_state():
    task = Task.from_state(
        {
            "workflow_status": "awaiting_ci",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "ci_checks": [],
            "ci_complete": True,
        }
    )

    assert task.record_ci() == {
        "workflow_status": "no_ci_checks",
        "waiting_for": "operator_decision",
        "ci_status": "unavailable",
    }


def test_submitted_changes_requested_review_routes_matching_pr_to_repair():
    task = Task.from_state(
        {
            "workflow_status": "awaiting_human_merge",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "ci_status": "success",
            "ci_checks": [{"status": "success"}],
            "ci_complete": True,
            "review_feedback": "Please revert the unrelated README change.",
        }
    )

    assert task.record_changes_requested() == {
        "workflow_status": "repair_needed",
        "waiting_for": "repair_changes",
        "ci_status": "pending",
        "ci_checks": [],
        "ci_complete": False,
        "no_ci_resolution": None,
        "review_feedback": "Please revert the unrelated README change.",
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


def test_changes_requested_review_ignores_wrong_pr_or_empty_feedback():
    base = {
        "workflow_status": "awaiting_human_merge",
        "pull_request_id": "pr-1",
        "ci_status": "success",
        "review_feedback": "",
    }

    assert Task.from_state(
        {**base, "event_pull_request_id": "pr-other"}
    ).record_changes_requested() == {"event_status": "ignored"}
    assert Task.from_state(
        {**base, "event_pull_request_id": "pr-1"}
    ).record_changes_requested() == {"event_status": "ignored"}


def test_human_reply_must_target_the_pending_inline_clarification():
    task = Task.from_state(
        {
            "workflow_status": "repair_unresolved",
            "waiting_for": "human_feedback",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "pending_question": "Should this README change be reverted?",
            "pending_question_comment_id": "comment-72",
            "pending_question_source_comment_id": "4461111142",
            "human_feedback_reply": "Yes, revert it.",
            "reply_to_comment_id": "4461111143",
            "reply_comment_id": "4461111144",
            "reply_action": "created",
        }
    )

    assert task.record_human_feedback_reply() == {"event_status": "ignored"}


def test_human_reply_to_inline_thread_root_resumes_pending_clarification():
    task = Task.from_state(
        {
            "workflow_status": "repair_unresolved",
            "waiting_for": "human_feedback",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "pending_question": "Should these README lines be restored?",
            "pending_question_comment_id": "4462086765",
            "pending_question_source_comment_id": "4461111142",
            "human_feedback_reply": "/agent Restore these README lines.",
            "reply_to_comment_id": "4461111142",
            "reply_comment_id": "4461111144",
            "reply_action": "created",
        }
    )

    assert task.record_human_feedback_reply() == {
        "workflow_status": "repair_needed",
        "waiting_for": "repair_changes",
        "repair_source_comment_id": "4461111142",
        "review_comment_ids": ["4461111142"],
        "repair_direction": "Restore these README lines.",
        "review_feedback": "Human clarification:\nRestore these README lines.",
        "human_feedback_reply": "Restore these README lines.",
        "last_human_feedback_reply_comment_id": "4461111144",
        "last_human_feedback_reply_to_comment_id": "4461111142",
        "clarification_base_review_feedback": "",
        "clarification_base_repair_direction": "",
        "event_status": "human_feedback_reply",
    }


def test_unrelated_edited_reply_is_ignored_even_for_the_active_question():
    task = Task.from_state(
        {
            "workflow_status": "awaiting_ci",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "pending_question": "Should these README lines be restored?",
            "pending_question_comment_id": "comment-72",
            "pending_question_source_comment_id": "4461111142",
            "last_human_feedback_reply_comment_id": "4461111144",
            "last_human_feedback_reply_to_comment_id": "4461111142",
            "human_feedback_reply": "Ignore this unrelated edit.",
            "reply_action": "edited",
            "reply_comment_id": "4461111199",
            "reply_to_comment_id": "4461111142",
        }
    )

    assert task.record_human_feedback_reply() == {"event_status": "ignored"}


def test_edited_reply_is_ignored_when_no_clarification_is_active():
    task = Task.from_state(
        {
            "workflow_status": "awaiting_ci",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "last_human_feedback_reply_comment_id": "4461111144",
            "last_human_feedback_reply_to_comment_id": "4461111142",
            "reply_action": "edited",
            "reply_comment_id": "4461111144",
            "reply_to_comment_id": "4461111142",
            "human_feedback_reply": "An answer edit without an active question.",
        }
    )

    assert task.record_human_feedback_reply() == {"event_status": "ignored"}


def test_agent_repair_request_resumes_unresolved_task_without_becoming_a_reply():
    task = Task.from_state(
        {
            "workflow_status": "repair_unresolved",
            "waiting_for": "human_feedback",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "event_head_sha": "head-2",
            "repair_direction": "Revert the unrelated README change.",
            "pending_question": "Should the README change be reverted?",
            "pending_question_comment_id": "comment-72",
            "human_feedback_reply": "stale reply",
        }
    )

    transition = task.record_agent_repair_requested()

    assert transition["workflow_status"] == "repair_needed"
    assert transition["event_status"] == "agent_repair_requested"
    assert transition["repair_direction"] == "Revert the unrelated README change."
    assert transition["review_feedback"] == ""
    assert transition["human_feedback_reply"] == ""
    assert transition["pending_question_comment_id"] == ""


@pytest.mark.parametrize(
    ("event_pull_request_id", "event_head_sha"),
    [("pr-other", "head-2"), ("pr-1", "")],
)
def test_agent_repair_request_requires_matching_pr_and_current_head(
    event_pull_request_id, event_head_sha
):
    task = Task.from_state(
        {
            "workflow_status": "awaiting_ci",
            "pull_request_id": "pr-1",
            "event_pull_request_id": event_pull_request_id,
            "event_head_sha": event_head_sha,
            "repair_direction": "Revert the unrelated README change.",
        }
    )

    assert task.record_agent_repair_requested() == {"event_status": "ignored"}


def test_no_ci_completion_requires_a_matching_audited_operator_decision():
    unresolved = Task.from_state(
        {
            "workflow_status": "awaiting_human_merge",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "ci_checks": [],
            "ci_complete": True,
            "ci_status": "unavailable",
            "merged": True,
        }
    )
    resolved = Task.from_state(
        {
            "workflow_status": "awaiting_human_merge",
            "pull_request_id": "pr-1",
            "event_pull_request_id": "pr-1",
            "ci_checks": [],
            "ci_complete": True,
            "ci_status": "unavailable",
            "no_ci_resolution": {
                "actor": "verified-owner",
                "decided_at": "2026-11-03T12:00:00+00:00",
                "decision": "proceed_without_ci",
                "pull_request_id": "pr-1",
            },
            "merged": True,
        }
    )

    assert not unresolved.can_complete()
    assert resolved.can_complete()
