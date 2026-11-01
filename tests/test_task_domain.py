import pytest

from codefactory.langgraph_service.task import Task


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
        ([], False, "unavailable"),
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
