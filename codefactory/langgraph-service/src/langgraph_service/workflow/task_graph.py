"""A bounded, event-driven task lifecycle with injectable external operations.

Operations are intentionally absent by default. A runtime may inject bounded
implementations for context, planning, risk assessment, change preparation, PR
publication, and repair. This module itself performs no I/O, shell execution,
or GitHub API calls. Callers persist returned state and supply it with later
normalized CI and merge events.
"""

from collections.abc import Callable, Mapping
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from langgraph_service.workflow.operations import Operation, WorkflowOperations
from langgraph_service.workflow.task import Task

TASK_GRAPH_NODE_NAMES = frozenset(
    {
        "dispatch",
        "validate_task",
        "context",
        "plan",
        "risk",
        "change",
        "publish_pr",
        "await_ci",
        "record_ci",
        "record_review",
        "record_human_feedback_reply",
        "repair",
        "human_merge_boundary",
        "record_merge",
        "record_completion",
        "record_no_ci_resolution",
    }
)


class TaskState(TypedDict, total=False):
    task_id: str
    agent_config: dict[str, object]
    agent_roster_snapshot: dict[str, object]
    webhook_delivery_id: str
    event_id: str
    task_status: str
    event_type: str
    command_comment_id: str
    target_repositories: list[str]
    target_repository: str
    issue: dict[str, object]
    project_node_id: str
    workflow_status: str
    waiting_for: str
    event_status: str
    context: dict[str, object]
    plan: dict[str, object]
    risk: dict[str, object]
    change: dict[str, object]
    pull_request: dict[str, object]
    pull_request_id: str
    event_pull_request_id: str
    project_item_id: str
    project_update: dict[str, object]
    merged: bool
    completion_confirmed: bool
    ci_checks: list[Mapping[str, object]]
    ci_complete: bool
    ci_status: str
    no_ci_resolution: dict[str, object]
    review_feedback: str
    review_comment_ids: list[str]
    inline_review_contexts: dict[str, dict[str, object]]
    repair_direction: str
    repair_source_comment_id: str
    inline_review_context: dict[str, object]
    pending_question: str
    pending_question_comment_id: str
    pending_question_source_comment_id: str
    human_feedback_reply: str
    reply_to_comment_id: str
    reply_comment_id: str
    reply_action: str
    last_human_feedback_reply_comment_id: str
    last_human_feedback_reply_to_comment_id: str
    clarification_base_review_feedback: str
    clarification_base_repair_direction: str
    reviewed_head_sha: str
    head_sha: str
    ci_head_sha: str
    event_head_sha: str
    operator_actor: str
    operator_decision: str
    operator_decided_at: str


def _operation_node(
    stage: str,
    result_key: str,
    operation: Operation | None,
    *,
    request_human_feedback: Operation | None = None,
) -> Callable[[TaskState], dict[str, object]]:
    def run(state: TaskState) -> dict[str, object]:
        if operation is None:
            return {
                "workflow_status": "pending_external_operation",
                "waiting_for": stage,
            }

        result = dict(operation(dict(state)))
        if stage == "repair_changes" and result.get("outcome") == "repair_unresolved":
            question = result.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("unresolved repair must include a human question")
            source_comment_id = result.get("source_review_comment_id")
            if source_comment_id is not None or state.get("event_type") == (
                "agent_repair_requested"
            ):
                review_comment_ids = state.get("review_comment_ids", [])
                if (
                    not isinstance(source_comment_id, str)
                    or not source_comment_id.isdecimal()
                    or int(source_comment_id) <= 0
                    or not isinstance(review_comment_ids, list)
                    or source_comment_id not in review_comment_ids
                ):
                    raise ValueError(
                        "human feedback question has an invalid source comment"
                    )
            if request_human_feedback is None:
                unresolved_state = {
                    result_key: result,
                    "workflow_status": "pending_external_operation",
                    "waiting_for": "request_human_feedback",
                }
                inline_context = _selected_inline_review_context(
                    state, source_comment_id
                )
                if inline_context is not None:
                    unresolved_state["inline_review_context"] = inline_context
                return {
                    **unresolved_state,
                }
            confirmation = request_human_feedback(
                {
                    **state,
                    "repair_question": question.strip(),
                    "source_review_comment_id": result.get("source_review_comment_id"),
                }
            )
            comment_id = (
                confirmation.get("comment_id")
                if isinstance(confirmation, Mapping)
                else None
            )
            if not isinstance(comment_id, (str, int)) or isinstance(comment_id, bool):
                raise ValueError("human feedback question was not confirmed")
            if not str(comment_id).strip():
                raise ValueError("human feedback question was not confirmed")
            unresolved_state = {
                result_key: result,
                "pending_question": question.strip(),
                "pending_question_comment_id": str(comment_id),
                "pending_question_source_comment_id": source_comment_id or "",
                "last_human_feedback_reply_comment_id": "",
                "last_human_feedback_reply_to_comment_id": "",
                "clarification_base_review_feedback": "",
                "clarification_base_repair_direction": "",
                "workflow_status": "repair_unresolved",
                "waiting_for": "human_feedback",
                "event_status": "repair_unresolved",
            }
            inline_context = _selected_inline_review_context(state, source_comment_id)
            if inline_context is not None:
                unresolved_state["inline_review_context"] = inline_context
            return unresolved_state
        return {
            result_key: result,
            "workflow_status": "running",
            "waiting_for": "",
        }

    return run


def _selected_inline_review_context(
    state: TaskState, source_comment_id: object
) -> dict[str, object] | None:
    contexts = state.get("inline_review_contexts")
    context = (
        contexts.get(source_comment_id)
        if isinstance(contexts, Mapping) and isinstance(source_comment_id, str)
        else None
    )
    return dict(context) if isinstance(context, Mapping) else None


def build_task_graph(
    operations: WorkflowOperations | None = None,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    publish_pr_operation: Operation | None = None,
):
    """Build the lifecycle graph; injected operations are never provided here."""
    operations = operations or WorkflowOperations()
    graph = StateGraph(TaskState)

    def dispatch(state: TaskState) -> dict[str, str]:
        if state.get("event_type") in {
            "ready",
            "ci",
            "merged",
            "completion_confirmed",
            "resolve_no_ci",
            "review_changes_requested",
            "human_feedback_reply",
            "agent_repair_requested",
        }:
            return {}
        return {"workflow_status": "invalid_event"}

    def event_route(state: TaskState) -> str:
        event_type = state.get("event_type")
        return (
            event_type
            if event_type
            in {
                "ready",
                "ci",
                "merged",
                "completion_confirmed",
                "resolve_no_ci",
                "review_changes_requested",
                "human_feedback_reply",
                "agent_repair_requested",
            }
            else "invalid"
        )

    def validate_task(state: TaskState) -> dict[str, object]:
        task = Task.from_state(state)
        if task.status != "ready":
            return {"workflow_status": "task_not_ready"}
        repository = task.ready_repository()
        if repository is None:
            return {"workflow_status": "invalid_repository_count"}
        return {
            "target_repository": repository,
            "workflow_status": "running",
        }

    def operation_route(state: TaskState) -> str:
        return "next" if state.get("workflow_status") == "running" else "end"

    def record_ci(state: TaskState) -> dict[str, str]:
        transition = Task.from_state(state).record_ci()
        if (
            transition.get("workflow_status") == "ci_succeeded"
            and state.get("merged") is True
        ):
            return {
                "workflow_status": "awaiting_human_merge",
                "event_status": "completion_requested",
            }
        return transition

    def record_no_ci_resolution(state: TaskState) -> dict[str, object]:
        return Task.from_state(state).resolve_no_ci(
            actor=state.get("operator_actor"),
            decided_at=state.get("operator_decided_at"),
            decision=state.get("operator_decision"),
        )

    def record_review(state: TaskState) -> dict[str, object]:
        task = Task.from_state(state)
        is_agent_repair = state.get("event_type") == "agent_repair_requested"
        transition = (
            task.record_agent_repair_requested()
            if is_agent_repair
            else task.record_changes_requested()
        )
        if transition.get("workflow_status") != "repair_needed":
            return transition
        head_sha = state.get("event_head_sha")
        if is_agent_repair and (not isinstance(head_sha, str) or not head_sha):
            raise ValueError(
                "agent repair event is missing the current pull request head"
            )
        if not isinstance(head_sha, str) or not head_sha:
            head_sha = state.get("head_sha")
        if not isinstance(head_sha, str) or not head_sha:
            pull_request = state.get("pull_request")
            head_sha = (
                pull_request.get("head_sha")
                if isinstance(pull_request, Mapping)
                else None
            )
        if not isinstance(head_sha, str) or not head_sha:
            raise ValueError("review event is missing the current pull request head")
        return {**transition, "reviewed_head_sha": head_sha}

    def record_human_feedback_reply(state: TaskState) -> dict[str, object]:
        transition = Task.from_state(state).record_human_feedback_reply()
        if transition.get("workflow_status") != "repair_needed":
            return transition
        return transition

    def ci_route(state: TaskState) -> str:
        if state.get("workflow_status") == "repair_needed":
            return "repair"
        if state.get("workflow_status") == "ci_succeeded":
            return "human_merge"
        return "end"

    def review_route(state: TaskState) -> str:
        return "repair" if state.get("workflow_status") == "repair_needed" else "end"

    def publish_route(state: TaskState) -> str:
        if state.get("workflow_status") == "repair_unresolved":
            return "end"
        if state.get("workflow_status") == "no_ci_checks":
            return "end"
        if state.get("workflow_status") == "repair_needed":
            return "repair"
        if state.get("workflow_status") == "ci_succeeded":
            return "human_merge"
        return "await_ci"

    def await_ci(state: TaskState) -> dict[str, str]:
        return {"workflow_status": "awaiting_ci", "waiting_for": "github_ci"}

    def human_merge_boundary(state: TaskState) -> dict[str, str]:
        return {
            "workflow_status": "awaiting_human_merge",
            "waiting_for": "human_merge",
        }

    def record_merge(state: TaskState) -> dict[str, str]:
        if not Task.from_state(state).can_complete():
            return {"event_status": "ignored_merge_event"}
        return {"event_status": "completion_requested"}

    def record_completion(state: TaskState) -> dict[str, str]:
        if (
            not Task.from_state(state).can_complete()
            or state.get("completion_confirmed") is not True
        ):
            return {"event_status": "ignored_completion_confirmation"}
        return {
            "task_status": "done",
            "workflow_status": "done",
            "waiting_for": "",
            "event_status": "done",
        }

    def publish_pr(state: TaskState) -> dict[str, object]:
        operation = publish_pr_operation
        if operation is None:
            return {
                "workflow_status": "pending_external_operation",
                "waiting_for": "publish_pr",
            }
        result = dict(operation(dict(state)))
        pull_request_id = result.get("pull_request_id")
        if not pull_request_id:
            return {
                "pull_request": result,
                "workflow_status": "pending_external_operation",
                "waiting_for": "pull_request_id",
            }
        checks = result.get("ci_checks")
        ci_complete = result.get("ci_complete")
        if (
            not isinstance(checks, list)
            or any(not isinstance(check, Mapping) for check in checks)
            or not isinstance(ci_complete, bool)
        ):
            return {
                "pull_request": result,
                "workflow_status": "pending_external_operation",
                "waiting_for": "ci_snapshot",
            }
        head_sha = result.get("head_sha")
        ci_head_sha = result.get("ci_head_sha")
        reviewed_head_sha = state.get("reviewed_head_sha")
        if isinstance(reviewed_head_sha, str) and (
            not isinstance(head_sha, str)
            or not head_sha
            or head_sha == reviewed_head_sha
        ):
            return {
                "pull_request": result,
                "pull_request_id": str(pull_request_id),
                "head_sha": head_sha if isinstance(head_sha, str) else "",
                "ci_head_sha": ci_head_sha if isinstance(ci_head_sha, str) else "",
                "workflow_status": "repair_unresolved",
                "waiting_for": "human_feedback",
                "ci_status": "pending",
                "event_status": "repair_unresolved",
            }
        if not isinstance(head_sha, str) or not head_sha:
            return {
                "pull_request": result,
                "pull_request_id": str(pull_request_id),
                "workflow_status": "pending_external_operation",
                "waiting_for": "branch_head",
            }
        ci_state = {
            **state,
            "pull_request_id": str(pull_request_id),
            "head_sha": head_sha,
            "ci_checks": checks,
            "ci_complete": ci_complete,
        }
        ci_decision = (
            Task.from_state(ci_state).ci_decision()
            if ci_head_sha == head_sha
            else "pending"
        )
        if ci_decision == "no_checks":
            return {
                "pull_request": result,
                "pull_request_id": str(pull_request_id),
                "head_sha": head_sha,
                "ci_head_sha": ci_head_sha if isinstance(ci_head_sha, str) else "",
                "ci_checks": checks,
                "ci_complete": ci_complete,
                "ci_status": "unavailable",
                "workflow_status": "no_ci_checks",
                "waiting_for": "operator_decision",
            }
        if ci_decision == "failure":
            workflow_status = "repair_needed"
        elif ci_decision == "success":
            workflow_status = "ci_succeeded"
        else:
            workflow_status = "running"
        return {
            "pull_request": result,
            "pull_request_id": str(pull_request_id),
            "head_sha": head_sha,
            "ci_head_sha": ci_head_sha if isinstance(ci_head_sha, str) else "",
            "ci_checks": checks,
            "ci_complete": ci_complete,
            "ci_status": ci_decision,
            "workflow_status": workflow_status,
            "waiting_for": "",
        }

    nodes = {
        "dispatch": dispatch,
        "validate_task": validate_task,
        "context": _operation_node("context", "context", operations.gather_context),
        "plan": _operation_node("plan", "plan", operations.create_plan),
        "risk": _operation_node("risk", "risk", operations.assess_risk),
        "change": _operation_node(
            "change_preparation", "change", operations.prepare_changes
        ),
        "publish_pr": publish_pr,
        "await_ci": await_ci,
        "record_ci": record_ci,
        "record_review": record_review,
        "record_human_feedback_reply": record_human_feedback_reply,
        "record_no_ci_resolution": record_no_ci_resolution,
        "repair": _operation_node(
            "repair_changes",
            "change",
            operations.repair_changes,
            request_human_feedback=operations.request_human_feedback,
        ),
        "human_merge_boundary": human_merge_boundary,
        "record_merge": record_merge,
        "record_completion": record_completion,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)

    graph.add_edge(START, "dispatch")
    graph.add_conditional_edges(
        "dispatch",
        event_route,
        {
            "ready": "validate_task",
            "ci": "record_ci",
            "merged": "record_merge",
            "completion_confirmed": "record_completion",
            "resolve_no_ci": "record_no_ci_resolution",
            "review_changes_requested": "record_review",
            "agent_repair_requested": "record_review",
            "human_feedback_reply": "record_human_feedback_reply",
            "invalid": END,
        },
    )
    graph.add_conditional_edges(
        "validate_task",
        lambda state: "context" if state.get("workflow_status") == "running" else "end",
        {"context": "context", "end": END},
    )
    for current, following in (
        ("context", "plan"),
        ("plan", "risk"),
        ("risk", "change"),
        ("change", "publish_pr"),
    ):
        graph.add_conditional_edges(
            current,
            operation_route,
            {"next": following, "end": END},
        )
    graph.add_conditional_edges(
        "publish_pr",
        publish_route,
        {
            "await_ci": "await_ci",
            "repair": "repair",
            "human_merge": "human_merge_boundary",
            "end": END,
        },
    )
    graph.add_edge("await_ci", END)
    graph.add_conditional_edges(
        "record_ci",
        ci_route,
        {"repair": "repair", "human_merge": "human_merge_boundary", "end": END},
    )
    graph.add_conditional_edges(
        "record_review",
        review_route,
        {"repair": "repair", "end": END},
    )
    graph.add_conditional_edges(
        "record_human_feedback_reply",
        lambda state: (
            "repair" if state.get("workflow_status") == "repair_needed" else "end"
        ),
        {"repair": "repair", "end": END},
    )
    graph.add_conditional_edges(
        "repair",
        operation_route,
        {"next": "publish_pr", "end": END},
    )
    graph.add_edge("human_merge_boundary", END)
    graph.add_edge("record_merge", END)
    graph.add_edge("record_completion", END)
    graph.add_edge("record_no_ci_resolution", END)
    return graph.compile(checkpointer=checkpointer)
