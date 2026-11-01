"""Checkpointed task lifecycle service exposed to the GitHub service."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol, Self

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from codefactory.langgraph_service.operations import Operation, WorkflowOperations
from codefactory.langgraph_service.task_graph import build_task_graph


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
    ):
        if (database_path is None) == (database_url is None):
            raise ValueError("configure exactly one local path or PostgreSQL URL")
        self._database_path = database_path
        self._database_url = database_url
        self._operations = operations
        self._postgres_saver_factory = postgres_saver_factory
        self._publish_pr = publish_pr
        self._saver_context: AbstractContextManager[BaseCheckpointSaver] | None = None
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
            checkpointer.setup()
            self._graph = build_task_graph(
                self._operations,
                checkpointer=checkpointer,
                publish_pr_operation=self._publish_pr,
            )
        except BaseException as error:
            saver_context.__exit__(type(error), error, error.__traceback__)
            raise
        self._saver_context = saver_context
        return self

    def __exit__(self, exception_type, exception, traceback) -> bool | None:
        if self._saver_context is None:
            return None
        saver_context = self._saver_context
        self._saver_context = None
        self._graph = None
        return saver_context.__exit__(exception_type, exception, traceback)

    def invoke(self, event: Mapping[str, object]) -> Mapping[str, object]:
        if self._graph is None:
            raise RuntimeError("LangGraphTaskService must be entered before use")
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task lifecycle event must include a task_id")
        return self._graph.invoke(
            dict(event),
            config={"configurable": {"thread_id": task_id}},
        )
