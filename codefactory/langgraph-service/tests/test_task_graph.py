import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_service.workflow.operations import WorkflowOperations
from langgraph_service.workflow.task_graph import build_task_graph


def _operations(*, repair_changes=None, request_human_feedback=None):
    return WorkflowOperations(
        gather_context=lambda _: {"paths": ["src/example.py"]},
        create_plan=lambda _: {"steps": ["update behavior", "validate"]},
        assess_risk=lambda _: {"summary": "Review the focused behavior change."},
        prepare_changes=lambda _: {"summary": "Prepared a bounded patch."},
        repair_changes=repair_changes,
        request_human_feedback=request_human_feedback,
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


def _graph(operations=None, *, checkpointer=None, publish_pr_operation=None):
    return build_task_graph(
        operations or _operations(),
        checkpointer=checkpointer,
        publish_pr_operation=publish_pr_operation
        or (
            lambda _: {
                "pull_request_id": "pr-17",
                "head_sha": "head-1",
                "ci_head_sha": "head-1",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            }
        ),
    )


def _ci_event(state, checks, *, complete=True):
    return {
        **state,
        "event_type": "ci",
        "event_pull_request_id": "pr-17",
        "event_head_sha": state.get("head_sha"),
        "ci_checks": checks,
        "ci_complete": complete,
    }


def test_agent_repair_source_id_reaches_checkpoint_and_clarification():
    repair_inputs = []
    asked = []
    source_comment_id = "4461111142"
    operations = _operations(
        repair_changes=lambda state: (
            repair_inputs.append(dict(state))
            or {
                "outcome": "repair_unresolved",
                "question": "Which behavior should this correction preserve?",
                "source_review_comment_id": source_comment_id,
            }
        ),
        request_human_feedback=lambda state: (
            asked.append(dict(state)) or {"comment_id": "comment-1"}
        ),
    )
    checkpointer = InMemorySaver()
    graph = _graph(operations, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "task-1"}}
    state = graph.invoke(
        {
            "task_id": "task-1",
            "task_status": "ready",
            "event_type": "ready",
            "target_repositories": ["example/project"],
        },
        config=config,
    )

    waiting = graph.invoke(
        {
            **state,
            "event_id": "agent-repair-event-1",
            "event_type": "agent_repair_requested",
            "event_pull_request_id": "pr-17",
            "event_head_sha": "head-1",
            "repair_direction": "Revert the unrelated change.",
            "review_comment_ids": [source_comment_id],
        },
        config=config,
    )

    checkpoint = graph.get_state(config)
    assert repair_inputs[0]["review_comment_ids"] == [source_comment_id]
    assert asked[0]["source_review_comment_id"] == source_comment_id
    assert waiting["pending_question_source_comment_id"] == source_comment_id
    assert checkpoint.values["review_comment_ids"] == [source_comment_id]
    assert waiting["workflow_status"] == "repair_unresolved"


def test_agent_repair_without_selected_source_does_not_request_feedback():
    asked = []
    operations = _operations(
        repair_changes=lambda _: {
            "outcome": "repair_unresolved",
            "question": "Which behavior should this correction preserve?",
        },
        request_human_feedback=lambda state: (
            asked.append(dict(state)) or {"comment_id": "comment-1"}
        ),
    )
    graph = _graph(operations)
    state = _ready_state(graph)

    with pytest.raises(ValueError, match="invalid source comment"):
        graph.invoke(
            {
                **state,
                "event_id": "agent-repair-event-missing-source",
                "event_type": "agent_repair_requested",
                "event_pull_request_id": "pr-17",
                "event_head_sha": "head-1",
                "repair_direction": "Revert the unrelated change.",
                "review_comment_ids": ["4461111142"],
            }
        )

    assert asked == []


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


def test_no_ci_requires_owner_resolution_and_never_becomes_ci_success():
    graph = build_task_graph(
        _operations(),
        publish_pr_operation=lambda _: {
            "pull_request_id": "pr-17",
            "head_sha": "head-1",
            "ci_head_sha": "head-1",
            "ci_checks": [],
            "ci_complete": True,
        },
    )
    state = _ready_state(graph)

    assert state["workflow_status"] == "no_ci_checks"
    assert state["waiting_for"] == "operator_decision"
    assert state["ci_status"] == "unavailable"

    rejected = graph.invoke(
        {
            **state,
            "event_type": "resolve_no_ci",
            "operator_actor": "unverified actor",
            "operator_decision": "proceed_without_ci",
            "operator_decided_at": "2026-11-03T12:00:00+00:00",
        }
    )
    resolved = graph.invoke(
        {
            **state,
            "event_type": "resolve_no_ci",
            "operator_actor": "verified-owner",
            "operator_decision": "proceed_without_ci",
            "operator_decided_at": "2026-11-03T12:00:00+00:00",
        }
    )
    merged = graph.invoke(
        {
            **resolved,
            "event_type": "merged",
            "event_pull_request_id": "pr-17",
            "merged": True,
        }
    )

    assert rejected["workflow_status"] == "no_ci_checks"
    assert resolved["event_status"] == "no_ci_resolved"
    assert resolved["event_type"] == "resolve_no_ci"
    assert resolved["ci_status"] == "unavailable"
    assert resolved["no_ci_resolution"] == {
        "actor": "verified-owner",
        "decided_at": "2026-11-03T12:00:00+00:00",
        "decision": "proceed_without_ci",
        "pull_request_id": "pr-17",
    }
    assert merged["event_status"] == "completion_requested"


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


def test_changes_requested_review_routes_human_merge_back_to_repair():
    feedback_seen = []
    operations = _operations(
        repair_changes=lambda state: (
            feedback_seen.append(state["review_feedback"]) or {"fixed": True}
        )
    )
    publication_results = iter(
        [
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-1",
                "ci_head_sha": "head-1",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-2",
                "ci_head_sha": "head-2",
                "ci_checks": [{"name": "required-tests", "status": "success"}],
                "ci_complete": True,
            },
        ]
    )
    graph = _graph(
        operations,
        publish_pr_operation=lambda _: next(publication_results),
    )
    state = _ready_state(graph)
    human_merge = graph.invoke(
        _ci_event(state, [{"name": "required-tests", "status": "success"}])
    )

    repaired = graph.invoke(
        {
            **human_merge,
            "event_type": "review_changes_requested",
            "event_pull_request_id": "pr-17",
            "review_feedback": "Revert the unrelated README change.",
        }
    )

    assert human_merge["workflow_status"] == "awaiting_human_merge"
    assert feedback_seen == ["Revert the unrelated README change."]
    assert repaired["change"] == {"fixed": True}
    assert repaired["head_sha"] == "head-2"
    assert repaired["ci_head_sha"] == "head-2"
    assert repaired["workflow_status"] == "awaiting_human_merge"


def test_unresolved_repair_posts_one_question_waits_and_resumes_with_reply():
    asked = []
    repair_inputs = []
    outcomes = iter(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Should the requested behavior apply to archived records too?",
                "source_review_comment_id": "4461111142",
            },
            {"fixed": True},
            {"fixed": True},
        ]
    )
    operations = _operations(
        repair_changes=lambda state: (
            repair_inputs.append(dict(state)) or next(outcomes)
        ),
        request_human_feedback=lambda state: (
            asked.append(
                (
                    state["repair_question"],
                    state["event_id"],
                    state["source_review_comment_id"],
                )
            )
            or {"comment_id": "comment-1"}
        ),
    )
    publications = iter(
        [
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-1",
                "ci_head_sha": "head-1",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-2",
                "ci_head_sha": "head-2",
                "ci_checks": [{"name": "required-tests", "status": "success"}],
                "ci_complete": True,
            },
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-3",
                "ci_head_sha": "head-3",
                "ci_checks": [{"name": "required-tests", "status": "success"}],
                "ci_complete": True,
            },
        ]
    )
    graph = _graph(
        operations,
        publish_pr_operation=lambda _: next(publications),
    )
    state = _ready_state(graph)
    waiting = graph.invoke(
        {
            **state,
            "event_id": "event-review-1",
            "event_type": "review_changes_requested",
            "event_pull_request_id": "pr-17",
            "review_feedback": "Clarify how archived records should be handled.",
            "review_comment_ids": ["4461111142"],
        }
    )

    assert asked == [
        (
            "Should the requested behavior apply to archived records too?",
            "event-review-1",
            "4461111142",
        )
    ]
    assert waiting["workflow_status"] == "repair_unresolved"
    assert waiting["waiting_for"] == "human_feedback"
    assert waiting["pending_question"].startswith("Should the requested behavior")
    assert waiting["pending_question_comment_id"] == "comment-1"
    assert waiting["pending_question_source_comment_id"] == "4461111142"

    resumed = graph.invoke(
        {
            **waiting,
            "event_id": "reply-created-event",
            "event_type": "human_feedback_reply",
            "event_pull_request_id": "pr-17",
            "human_feedback_reply": "Yes, archived records should be included.",
            "reply_to_comment_id": "comment-1",
            "reply_comment_id": "4461111143",
            "reply_action": "created",
        }
    )

    assert len(asked) == 1
    assert (
        "Human clarification:\nYes, archived records should be included."
        in (repair_inputs[1]["review_feedback"])
    )
    assert repair_inputs[1]["repair_source_comment_id"] == "4461111142"
    assert repair_inputs[1]["review_comment_ids"] == ["4461111142"]
    assert (
        "Yes, archived records should be included."
        in repair_inputs[1]["repair_direction"]
    )
    assert resumed["change"] == {"fixed": True}

    revised = graph.invoke(
        {
            **resumed,
            "event_id": "reply-edited-event",
            "event_type": "human_feedback_reply",
            "event_pull_request_id": "pr-17",
            "human_feedback_reply": "Include archived and soft-deleted records.",
            "reply_to_comment_id": "comment-1",
            "reply_comment_id": "4461111143",
            "reply_action": "edited",
        }
    )

    assert len(asked) == 1
    assert len(repair_inputs) == 3
    assert (
        "Yes, archived records should be included."
        not in repair_inputs[2]["review_feedback"]
    )
    assert (
        "Include archived and soft-deleted records."
        in repair_inputs[2]["review_feedback"]
    )
    assert revised["workflow_status"] == "awaiting_human_merge"

    ignored = graph.invoke(
        {
            **revised,
            "event_id": "unrelated-reply-edited-event",
            "event_type": "human_feedback_reply",
            "event_pull_request_id": "pr-17",
            "human_feedback_reply": "An unrelated comment edit.",
            "reply_to_comment_id": "comment-1",
            "reply_comment_id": "4461111199",
            "reply_action": "edited",
        }
    )

    assert ignored["event_status"] == "ignored"
    assert len(repair_inputs) == 3
    assert resumed["workflow_status"] == "awaiting_human_merge"


def test_new_review_resumes_an_unresolved_repair():
    repair_inputs = []
    outcomes = iter(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Which README connectors should remain?",
            },
            {"fixed": True},
        ]
    )
    publications = iter(
        [
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-1",
                "ci_head_sha": "head-1",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-2",
                "ci_head_sha": "head-2",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
        ]
    )
    operations = _operations(
        repair_changes=lambda state: (
            repair_inputs.append(dict(state)) or next(outcomes)
        ),
        request_human_feedback=lambda _: {"comment_id": "comment-1"},
    )
    graph = _graph(
        operations,
        publish_pr_operation=lambda _: next(publications),
    )
    state = _ready_state(graph)
    unresolved = graph.invoke(
        {
            **state,
            "event_id": "review-1",
            "event_type": "review_changes_requested",
            "event_pull_request_id": "pr-17",
            "review_feedback": "The connector list is unclear.",
        }
    )

    resumed = graph.invoke(
        {
            **unresolved,
            "event_id": "review-2",
            "event_type": "review_changes_requested",
            "event_pull_request_id": "pr-17",
            "event_head_sha": "reviewed-head-2",
            "review_feedback": "Unexpected changes. Reverts.",
        }
    )

    assert unresolved["workflow_status"] == "repair_unresolved"
    assert [item["review_feedback"] for item in repair_inputs] == [
        "The connector list is unclear.",
        "Unexpected changes. Reverts.",
    ]
    assert resumed["change"] == {"fixed": True}
    assert resumed["reviewed_head_sha"] == "reviewed-head-2"
    assert resumed["head_sha"] == "head-2"
    assert resumed["pending_question"] == ""
    assert resumed["workflow_status"] == "awaiting_ci"


def test_agent_repair_direction_resumes_unresolved_task_as_instruction():
    repair_inputs = []
    outcomes = iter(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Should the README change be reverted?",
            },
            {"fixed": True},
        ]
    )
    operations = _operations(
        repair_changes=lambda state: (
            repair_inputs.append(dict(state)) or next(outcomes)
        ),
        request_human_feedback=lambda _: {"comment_id": "comment-1"},
    )
    publications = iter(
        [
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-1",
                "ci_head_sha": "head-1",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-2",
                "ci_head_sha": "head-2",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
        ]
    )
    graph = _graph(
        operations,
        publish_pr_operation=lambda _: next(publications),
    )
    state = _ready_state(graph)
    unresolved = graph.invoke(
        {
            **state,
            "event_id": "review-1",
            "event_type": "review_changes_requested",
            "event_pull_request_id": "pr-17",
            "event_head_sha": "head-1",
            "review_feedback": "The intended change is unclear.",
        }
    )

    resumed = graph.invoke(
        {
            **unresolved,
            "event_type": "agent_repair_requested",
            "event_pull_request_id": "pr-17",
            "event_head_sha": "head-1",
            "repair_direction": "Revert the unrelated README change.",
            "inline_review_context": {
                "path": "README.md",
                "line": 34,
                "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
            },
        }
    )

    assert unresolved["workflow_status"] == "repair_unresolved"
    assert repair_inputs[1]["repair_direction"] == "Revert the unrelated README change."
    assert repair_inputs[1]["inline_review_context"]["path"] == "README.md"
    assert repair_inputs[1]["review_feedback"] == ""
    assert repair_inputs[1]["human_feedback_reply"] == ""
    assert repair_inputs[1]["pending_question"] == ""
    assert resumed["reviewed_head_sha"] == "head-1"
    assert resumed["head_sha"] == "head-2"
    assert resumed["workflow_status"] == "awaiting_ci"


@pytest.mark.parametrize("stale_source_comment_id", ["", "6619605094"])
def test_agent_reply_supersedes_stale_clarification_and_keeps_inline_root(
    stale_source_comment_id,
):
    repair_inputs = []
    feedback_requests = []
    source_comment_id = "4461111142"
    command_comment_id = "4462864676"
    inline_context = {
        "path": "README.md",
        "line": 34,
        "start_line": 28,
        "side": "RIGHT",
        "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
    }
    operations = _operations(
        repair_changes=lambda state: (
            repair_inputs.append(dict(state))
            or {
                "outcome": "repair_unresolved",
                "question": "Should the connector correction change anything else?",
                "source_review_comment_id": source_comment_id,
            }
        ),
        request_human_feedback=lambda state: (
            feedback_requests.append(dict(state)) or {"comment_id": "followup-1"}
        ),
    )
    graph = _graph(operations)
    state = _ready_state(graph)

    waiting = graph.invoke(
        {
            **state,
            "event_id": "agent-reply-delivery",
            "event_type": "agent_repair_requested",
            "event_pull_request_id": "pr-17",
            "event_head_sha": "head-1",
            "workflow_status": "repair_unresolved",
            "waiting_for": "human_feedback",
            "pending_question": "A stale global clarification.",
            "pending_question_comment_id": "6619605093",
            "pending_question_source_comment_id": stale_source_comment_id,
            "human_feedback_reply": "A stale answer.",
            "clarification_base_review_feedback": "Stale review feedback.",
            "clarification_base_repair_direction": "Stale repair direction.",
            "repair_direction": (
                "Restore the README tree block to the PR base version. "
                "Keep all entries; only restore the original tree connector characters."
            ),
            "review_comment_ids": [source_comment_id],
            "command_comment_id": command_comment_id,
            "inline_review_context": inline_context,
        }
    )

    assert repair_inputs[0]["repair_direction"].startswith(
        "Restore the README tree block"
    )
    assert repair_inputs[0]["review_comment_ids"] == [source_comment_id]
    assert repair_inputs[0]["command_comment_id"] == command_comment_id
    assert repair_inputs[0]["inline_review_context"] == inline_context
    assert repair_inputs[0]["pending_question"] == ""
    assert repair_inputs[0]["pending_question_comment_id"] == ""
    assert repair_inputs[0]["pending_question_source_comment_id"] == ""
    assert repair_inputs[0]["human_feedback_reply"] == ""
    assert repair_inputs[0]["clarification_base_review_feedback"] == ""
    assert repair_inputs[0]["clarification_base_repair_direction"] == ""
    assert feedback_requests[0]["source_review_comment_id"] == source_comment_id
    assert waiting["pending_question_source_comment_id"] == source_comment_id
    assert waiting["inline_review_context"] == inline_context
    assert waiting["workflow_status"] == "repair_unresolved"


def test_unresolved_repair_waits_only_after_question_is_confirmed():
    operations = _operations(
        repair_changes=lambda _: {
            "outcome": "repair_unresolved",
            "question": "Which README connectors should remain?",
        },
        request_human_feedback=lambda _: {},
    )
    graph = _graph(operations)
    state = _ready_state(graph)

    with pytest.raises(ValueError, match="question was not confirmed"):
        graph.invoke(
            {
                **state,
                "event_id": "review-1",
                "event_type": "review_changes_requested",
                "event_pull_request_id": "pr-17",
                "review_feedback": "The connector list is unclear.",
            }
        )


def test_unchanged_review_head_with_old_green_ci_is_not_marked_repaired():
    operations = _operations(repair_changes=lambda _: {"fixed": True})
    publications = iter(
        [
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-reviewed",
                "ci_head_sha": "head-reviewed",
                "ci_checks": [{"name": "required-tests", "status": "pending"}],
                "ci_complete": False,
            },
            {
                "pull_request_id": "pr-17",
                "head_sha": "head-reviewed",
                "ci_head_sha": "head-reviewed",
                "ci_checks": [{"name": "required-tests", "status": "success"}],
                "ci_complete": True,
            },
        ]
    )
    graph = _graph(operations, publish_pr_operation=lambda _: next(publications))
    state = _ready_state(graph)
    human_merge = graph.invoke(
        _ci_event(state, [{"name": "required-tests", "status": "success"}])
    )

    unresolved = graph.invoke(
        {
            **human_merge,
            "event_type": "review_changes_requested",
            "event_pull_request_id": "pr-17",
            "review_feedback": "Please address the requested correction.",
        }
    )
    late_ci = graph.invoke(
        _ci_event(
            unresolved,
            [{"name": "required-tests", "status": "success"}],
        )
    )

    assert unresolved["reviewed_head_sha"] == "head-reviewed"
    assert unresolved["workflow_status"] == "repair_unresolved"
    assert unresolved["waiting_for"] == "human_feedback"
    assert late_ci["workflow_status"] == "repair_unresolved"


def test_ci_success_for_a_different_head_does_not_pass_the_merge_gate():
    graph = _graph()
    state = _ready_state(graph)

    result = graph.invoke(
        {
            **state,
            "event_type": "ci",
            "event_pull_request_id": "pr-17",
            "event_head_sha": "stale-head",
            "ci_checks": [{"name": "required-tests", "status": "success"}],
            "ci_complete": True,
        }
    )

    assert result["workflow_status"] == "awaiting_ci"
    assert result["event_status"] == "ignored"


@pytest.mark.parametrize(
    ("checks", "complete", "ci_status"),
    [
        ([{"name": "required-tests", "status": "pending"}], False, "pending"),
        ([], False, "pending"),
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
            "event_head_sha": state["head_sha"],
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
            "event_head_sha": ready["head_sha"],
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
