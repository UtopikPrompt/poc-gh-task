"""Checkpointed task lifecycle service exposed to the GitHub service."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol, Self

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from langgraph_service.config_store import PostgresAgentConfigStore
from langgraph_service.operations import Operation, WorkflowOperations
from langgraph_service.task_graph import build_task_graph


class TaskLifecycle(Protocol):
    def invoke(self, event: Mapping[str, object]) -> Mapping[str, object]: ...


class CompiledTaskGraph(Protocol):
    def invoke(
        self,
        input: Mapping[str, object],
        config: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class LangGraphTaskService:
    def __init__(
        self,
        database_path: Path | None = None,
        operations: WorkflowOperations | None = None,
        *,
        database_url: str | None = None,
        postgres_saver_factory: Any | None = None,
        publish_pr: Operation | None = None,
        agent_config_store: PostgresAgentConfigStore | None = None,
    ):
        if (database_path is None) == (database_url is None):
            raise ValueError("configure exactly one local path or PostgreSQL URL")
        self._database_path = database_path
        self._database_url = database_url
        self._operations = operations
        self._postgres_saver_factory = postgres_saver_factory
        self._publish_pr = publish_pr
        self._agent_config_store = agent_config_store
        self._saver_context: AbstractContextManager[BaseCheckpointSaver] | None = None
        self._checkpointer: BaseCheckpointSaver | None = None
        self._graph: CompiledTaskGraph | None = None

    def __enter__(self) -> Self:
        if self._database_url:
            saver_factory = self._postgres_saver_factory
            if saver_factory is None:
                try:
                    from langgraph.checkpoint.postgres import PostgresSaver
                except ImportError as error:
                    raise RuntimeError(
                        "PostgreSQL checkpoints require langgraph-checkpoint-postgres"
                    ) from error
                saver_factory = PostgresSaver
            saver_context = saver_factory.from_conn_string(self._database_url)
        else:
            saver_context = SqliteSaver.from_conn_string(str(self._database_path))
        checkpointer = saver_context.__enter__()
        try:
            self._graph = build_task_graph(
                self._operations,
                checkpointer=checkpointer,
                publish_pr_operation=self._publish_pr,
            )
        except BaseException as error:
            saver_context.__exit__(type(error), error, error.__traceback__)
            raise
        self._saver_context = saver_context
        self._checkpointer = checkpointer
        return self

    def __exit__(self, exception_type, exception, traceback) -> bool | None:
        if self._saver_context is None:
            return None
        saver_context = self._saver_context
        self._saver_context = None
        self._checkpointer = None
        self._graph = None
        return saver_context.__exit__(exception_type, exception, traceback)

    def invoke(self, event: Mapping[str, object]) -> Mapping[str, object]:
        if self._graph is None:
            raise RuntimeError("LangGraphTaskService must be entered before use")
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
            raise ValueError("task lifecycle event must include a valid task_id")
        graph_config = {"configurable": {"thread_id": task_id}}
        run_event = dict(event)
        run_event.pop("agent_config", None)
        if run_event.get("event_type") == "ready" and self._agent_config_store:
            snapshot = self._graph.get_state(graph_config)
            snapshot_config = getattr(snapshot, "config", None)
            configurable = (
                snapshot_config.get("configurable", {})
                if isinstance(snapshot_config, Mapping)
                else {}
            )
            has_checkpoint = isinstance(configurable, Mapping) and bool(
                configurable.get("checkpoint_id")
            )
            if not has_checkpoint:
                run_event["agent_config"] = (
                    self._agent_config_store.get().config.model_dump()
                )
        return self._graph.invoke(run_event, config=graph_config)

    def list_executions(self, *, limit: int = 100) -> list[dict[str, object]]:
        if self._checkpointer is None:
            raise RuntimeError("LangGraphTaskService must be entered before use")
        latest: dict[str, dict[str, object]] = {}
        for item in self._checkpointer.list(None, limit=1000):
            configurable = item.config.get("configurable", {})
            thread_id = configurable.get("thread_id")
            checkpoint = item.checkpoint
            if not isinstance(thread_id, str) or not isinstance(checkpoint, Mapping):
                continue
            summary = _execution_projection(
                thread_id=thread_id,
                values=checkpoint.get("channel_values"),
                checkpoint_id=checkpoint.get("id"),
                created_at=checkpoint.get("ts"),
                metadata=item.metadata,
            )
            previous = latest.get(thread_id)
            if previous is None or str(summary["created_at"] or "") > str(
                previous["created_at"] or ""
            ):
                latest[thread_id] = summary
        return sorted(
            latest.values(),
            key=lambda item: str(item["created_at"] or ""),
            reverse=True,
        )[:limit]

    def get_execution(self, thread_id: str) -> dict[str, object] | None:
        if self._graph is None:
            raise RuntimeError("LangGraphTaskService must be entered before use")
        history = self._graph.get_state_history(
            {"configurable": {"thread_id": thread_id}}, limit=20
        )
        checkpoints = [
            _execution_projection(
                thread_id=thread_id,
                values=snapshot.values,
                checkpoint_id=snapshot.config.get("configurable", {}).get(
                    "checkpoint_id"
                ),
                created_at=snapshot.created_at,
                metadata=snapshot.metadata,
            )
            for snapshot in history
        ]
        if not checkpoints:
            return None
        return {"thread_id": thread_id, "checkpoints": checkpoints}


def _execution_projection(
    *,
    thread_id: str,
    values: object,
    checkpoint_id: object,
    created_at: object,
    metadata: object,
) -> dict[str, object]:
    state = values if isinstance(values, Mapping) else {}
    safe_values = {
        field: value[:160] if isinstance(value, str) else None
        for field in (
            "workflow_status",
            "waiting_for",
            "task_status",
            "event_type",
            "event_status",
        )
        if isinstance((value := state.get(field)), str) and value
    }
    step = metadata.get("step") if isinstance(metadata, Mapping) else None
    return {
        "thread_id": thread_id[:200],
        "checkpoint_id": checkpoint_id[:200]
        if isinstance(checkpoint_id, str)
        else None,
        "created_at": created_at[:40] if isinstance(created_at, str) else None,
        "step": step if isinstance(step, int) and not isinstance(step, bool) else None,
        **safe_values,
    }
