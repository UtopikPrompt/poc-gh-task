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

from langgraph_service.operations import Operation, WorkflowOperations
from langgraph_service.task import Task


class TaskState(TypedDict, total=False):
    task_id: str
    agent_config: dict[str, object]
    webhook_delivery_id: str
    event_id: str
    task_status: str
    event_type: str
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


def _operation_node(
    stage: str,
    result_key: str,
    operation: Operation | None,
) -> Callable[[TaskState], dict[str, object]]:
    def run(state: TaskState) -> dict[str, object]:
        if operation is None:
            return {
                "workflow_status": "pending_external_operation",
                "waiting_for": stage,
            }

        result = dict(operation(dict(state)))
        return {
            result_key: result,
            "workflow_status": "running",
            "waiting_for": "",
        }

    return run


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
        }:
            return {}
        return {"workflow_status": "invalid_event"}

    def event_route(state: TaskState) -> str:
        event_type = state.get("event_type")
        return (
            event_type
            if event_type in {"ready", "ci", "merged", "completion_confirmed"}
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

    def ci_route(state: TaskState) -> str:
        if state.get("workflow_status") == "repair_needed":
            return "repair"
        if state.get("workflow_status") == "ci_succeeded":
            return "human_merge"
        return "end"

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
        return {
            "pull_request": result,
            "pull_request_id": str(pull_request_id),
            "workflow_status": "running",
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
        "repair": _operation_node(
            "repair_changes", "change", operations.repair_changes
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
        operation_route,
        {"next": "await_ci", "end": END},
    )
    graph.add_edge("await_ci", END)
    graph.add_conditional_edges(
        "record_ci",
        ci_route,
        {"repair": "repair", "human_merge": "human_merge_boundary", "end": END},
    )
    graph.add_conditional_edges(
        "repair",
        operation_route,
        {"next": "publish_pr", "end": END},
    )
    graph.add_edge("human_merge_boundary", END)
    graph.add_edge("record_merge", END)
    graph.add_edge("record_completion", END)
    return graph.compile(checkpointer=checkpointer)
