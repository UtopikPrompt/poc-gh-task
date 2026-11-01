import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_service.operations import WorkflowOperations
from langgraph_service.task_graph import build_task_graph


def _operations(*, repair_changes=None):
    return WorkflowOperations(
        gather_context=lambda _: {"paths": ["src/example.py"]},
        create_plan=lambda _: {"steps": ["update behavior", "validate"]},
        assess_risk=lambda _: {"summary": "Review the focused behavior change."},
        prepare_changes=lambda _: {"summary": "Prepared a bounded patch."},
        repair_changes=repair_changes,
    )


def _ready_state(graph):
    return graph.invoke(
        {
            "task_id": "task-1",
            "task_status": "ready",
            "event_type": "ready",
            "target_repositories": ["example/project"],
        }
    )


def _graph(operations=None, *, checkpointer=None):
    return build_task_graph(
        operations or _operations(),
        checkpointer=checkpointer,
        publish_pr_operation=lambda _: {"pull_request_id": "pr-17"},
    )


def _ci_event(state, checks, *, complete=True):
    return {
        **state,
        "event_type": "ci",
        "event_pull_request_id": "pr-17",
        "ci_checks": checks,
        "ci_complete": complete,
    }


@pytest.mark.parametrize("repositories", [[], ["one/repo", "two/repo"]])
def test_ready_task_requires_exactly_one_target_repository(repositories):
    called = []
    graph = build_task_graph(
        WorkflowOperations(gather_context=lambda _: called.append(True) or {})
    )

    result = graph.invoke(
        {
            "task_status": "ready",
            "event_type": "ready",
            "target_repositories": repositories,
        }
    )

    assert result["workflow_status"] == "invalid_repository_count"
    assert called == []


def test_valid_ready_task_runs_operations_until_ci_wait():
    result = _ready_state(_graph())

    assert result["target_repository"] == "example/project"
    assert result["pull_request_id"] == "pr-17"
    assert result["workflow_status"] == "awaiting_ci"
    assert result["task_status"] == "ready"


def test_missing_external_operation_stops_at_explicit_pending_state():
    result = _ready_state(build_task_graph())

    assert result["workflow_status"] == "pending_external_operation"
    assert result["waiting_for"] == "context"


def test_unknown_event_is_ignored_as_invalid():
    result = build_task_graph().invoke({"event_type": "comment"})

    assert result["workflow_status"] == "invalid_event"


def test_ci_failure_routes_to_repair_and_republishes_for_ci():
    repaired = []
    operations = _operations(
        repair_changes=lambda _: repaired.append(True) or {"fixed": True}
    )
    graph = _graph(operations)
    state = _ready_state(graph)

    result = graph.invoke(
        _ci_event(state, [{"name": "required-tests", "status": "failure"}])
    )

    assert repaired == [True]
    assert result["change"] == {"fixed": True}
    assert result["workflow_status"] == "awaiting_ci"


@pytest.mark.parametrize(
    ("checks", "complete", "ci_status"),
    [
        ([{"name": "required-tests", "status": "pending"}], False, "pending"),
        ([], False, "unavailable"),
    ],
)
def test_pending_or_unavailable_ci_keeps_merge_blocked(checks, complete, ci_status):
    graph = _graph()
    state = _ready_state(graph)

    result = graph.invoke(_ci_event(state, checks, complete=complete))

    assert result["workflow_status"] == "awaiting_ci"
    assert result["ci_status"] == ci_status
    assert result["task_status"] == "ready"


def test_successful_ci_reaches_human_boundary_without_completing_task():
    graph = _graph()
    state = _ready_state(graph)

    result = graph.invoke(
        _ci_event(state, [{"name": "required-tests", "status": "success"}])
    )

    assert result["workflow_status"] == "awaiting_human_merge"
    assert result["waiting_for"] == "human_merge"
    assert result["task_status"] == "ready"


def test_unrelated_or_late_ci_event_does_not_weaken_merge_gate():
    graph = _graph()
    state = _ready_state(graph)
    ci_passed = graph.invoke(
        _ci_event(state, [{"name": "required-tests", "status": "success"}])
    )

    unrelated = graph.invoke(
        {
            **ci_passed,
            "event_type": "ci",
            "event_pull_request_id": "pr-other",
            "ci_checks": [{"name": "required-tests", "status": "pending"}],
            "ci_complete": False,
        }
    )
    refreshed = graph.invoke(
        {
            **ci_passed,
            "event_type": "ci",
            "event_pull_request_id": "pr-17",
            "ci_checks": [{"name": "required-tests", "status": "success"}],
            "ci_complete": True,
        }
    )

    assert unrelated["workflow_status"] == "awaiting_human_merge"
    assert refreshed["workflow_status"] == "awaiting_human_merge"


def test_merge_requests_github_completion_without_marking_task_done():
    graph = _graph()
    state = _ready_state(graph)
    ci_passed = graph.invoke(
        _ci_event(state, [{"name": "required-tests", "status": "success"}])
    )

    not_merged = graph.invoke(
        {
            **ci_passed,
            "event_type": "merged",
            "event_pull_request_id": "pr-17",
            "merged": False,
        }
    )
    wrong_pr = graph.invoke(
        {
            **ci_passed,
            "event_type": "merged",
            "event_pull_request_id": "pr-other",
            "merged": True,
        }
    )
    completion_requested = graph.invoke(
        {
            **ci_passed,
            "event_type": "merged",
            "event_pull_request_id": "pr-17",
            "merged": True,
        }
    )
    completed = graph.invoke(
        {
            **completion_requested,
            "event_type": "completion_confirmed",
            "completion_confirmed": True,
        }
    )

    assert not_merged["task_status"] == "ready"
    assert wrong_pr["task_status"] == "ready"
    assert completion_requested["event_status"] == "completion_requested"
    assert completion_requested["task_status"] == "ready"
    assert completed["task_status"] == "done"
    assert completed["workflow_status"] == "done"


def test_checkpointer_resumes_workflow_across_separate_events():
    graph = _graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "task-1"}}

    ready = graph.invoke(
        {
            "task_id": "task-1",
            "task_status": "ready",
            "event_type": "ready",
            "target_repositories": ["example/project"],
        },
        config,
    )
    ci_passed = graph.invoke(
        {
            "event_type": "ci",
            "event_pull_request_id": "pr-17",
            "ci_checks": [{"name": "required-tests", "status": "success"}],
            "ci_complete": True,
        },
        config,
    )
    completed = graph.invoke(
        {
            "event_type": "merged",
            "event_pull_request_id": "pr-17",
            "merged": True,
        },
        config,
    )
    completion_requested = completed
    completed = graph.invoke(
        {
            "event_type": "completion_confirmed",
            "completion_confirmed": True,
        },
        config,
    )

    assert ready["workflow_status"] == "awaiting_ci"
    assert ci_passed["workflow_status"] == "awaiting_human_merge"
    assert completion_requested["event_status"] == "completion_requested"
    assert completed["task_status"] == "done"
