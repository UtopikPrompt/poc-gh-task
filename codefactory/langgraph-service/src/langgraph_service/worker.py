"""At-least-once durable event consumer for the LangGraph service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from langgraph_service.service import TaskLifecycle


class GitHubEventQueue(Protocol):
    def claim_event(self) -> dict[str, object] | None: ...

    def acknowledge_event(self, event_id: str, claim_token: str) -> None: ...

    def retry_event(self, event_id: str, claim_token: str, error: str) -> None: ...

    def complete_task(self, task_id: str) -> dict[str, object]: ...


class LangGraphEventWorker:
    def __init__(
        self,
        *,
        queue: GitHubEventQueue,
        lifecycle: TaskLifecycle,
        readiness: Callable[[], bool] | None = None,
    ):
        self._queue = queue
        self._lifecycle = lifecycle
        self._readiness = readiness

    def process_one(self) -> bool:
        if self._readiness is not None and not self._readiness():
            return False
        event = self._queue.claim_event()
        if event is None:
            return False
        event_id = event.get("event_id")
        claim_token = event.get("claim_token")
        payload = event.get("payload")
        if (
            not isinstance(event_id, str)
            or not isinstance(claim_token, str)
            or not isinstance(payload, Mapping)
        ):
            raise TypeError("GitHub service returned an invalid queue claim")

        try:
            event_input = dict(payload)
            event_input["event_id"] = event_id
            delivery_id = event.get("delivery_id")
            if isinstance(delivery_id, str):
                event_input["webhook_delivery_id"] = delivery_id
            task_id = event_input.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("queued event is missing task_id")
            result = self._lifecycle.invoke(event_input)
            if result.get("event_status") == "completion_requested":
                completion = self._queue.complete_task(task_id)
                if completion.get("completed") is not True:
                    raise RuntimeError("GitHub has not confirmed task completion")
                self._lifecycle.invoke(
                    {
                        "task_id": task_id,
                        "event_type": "completion_confirmed",
                        "completion_confirmed": True,
                    }
                )
            self._queue.acknowledge_event(event_id, claim_token)
            return True
        except Exception as error:
            self._queue.retry_event(event_id, claim_token, str(error))
            raise
