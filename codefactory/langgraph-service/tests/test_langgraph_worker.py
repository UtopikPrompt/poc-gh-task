import httpx
import pytest
from langgraph_service.github_client import (
    GitHubServiceClient,
    build_publish_pr_operation,
)
from langgraph_service.worker import LangGraphEventWorker


class FakeQueue:
    def __init__(self):
        self.event = {
            "event_id": "event-1",
            "delivery_id": "delivery-1",
            "claim_token": "claim-1",
            "payload": {"task_id": "task-1", "event_type": "ready"},
        }
        self.calls = []
        self.completion = {"completed": False}

    def claim_event(self):
        self.calls.append("claim")
        event = self.event
        self.event = None
        return event

    def acknowledge_event(self, event_id, claim_token):
        self.calls.append(("ack", event_id, claim_token))

    def retry_event(self, event_id, claim_token, error):
        self.calls.append(("retry", event_id, claim_token, error))

    def complete_task(self, task_id):
        self.calls.append(("complete", task_id))
        return self.completion


class FakeLifecycle:
    def __init__(self, result=None, error=None):
        self.result = result or {"workflow_status": "awaiting_ci"}
        self.error = error
        self.events = []

    def invoke(self, event):
        self.events.append(dict(event))
        if self.error:
            raise self.error
        return self.result


def test_worker_claims_invokes_and_acknowledges_with_event_identity():
    queue = FakeQueue()
    lifecycle = FakeLifecycle()
    worker = LangGraphEventWorker(queue=queue, lifecycle=lifecycle)

    assert worker.process_one() is True
    assert lifecycle.events == [
        {
            "task_id": "task-1",
            "event_type": "ready",
            "event_id": "event-1",
            "webhook_delivery_id": "delivery-1",
        }
    ]
    assert queue.calls == ["claim", ("ack", "event-1", "claim-1")]
    assert worker.process_one() is False


def test_worker_retries_graph_failure_without_acknowledging():
    queue = FakeQueue()
    lifecycle = FakeLifecycle(error=RuntimeError("temporary failure"))
    worker = LangGraphEventWorker(queue=queue, lifecycle=lifecycle)

    with pytest.raises(RuntimeError, match="temporary failure"):
        worker.process_one()

    assert queue.calls == [
        "claim",
        ("retry", "event-1", "claim-1", "temporary failure"),
    ]


def test_worker_only_records_done_after_github_completion_confirmation():
    queue = FakeQueue()
    queue.event["payload"] = {
        "task_id": "task-1",
        "event_type": "merged",
        "merged": True,
    }
    queue.completion = {"completed": True}
    lifecycle = FakeLifecycle(result={"event_status": "completion_requested"})
    worker = LangGraphEventWorker(queue=queue, lifecycle=lifecycle)

    assert worker.process_one() is True
    assert lifecycle.events[-1] == {
        "task_id": "task-1",
        "event_type": "completion_confirmed",
        "completion_confirmed": True,
    }
    assert queue.calls == [
        "claim",
        ("complete", "task-1"),
        ("ack", "event-1", "claim-1"),
    ]


def test_worker_retries_merge_event_while_github_revalidation_is_unconfirmed():
    queue = FakeQueue()
    queue.event["payload"] = {
        "task_id": "task-1",
        "event_type": "merged",
        "merged": True,
    }
    lifecycle = FakeLifecycle(result={"event_status": "completion_requested"})
    worker = LangGraphEventWorker(queue=queue, lifecycle=lifecycle)

    with pytest.raises(RuntimeError, match="not confirmed"):
        worker.process_one()

    assert queue.calls == [
        "claim",
        ("complete", "task-1"),
        ("retry", "event-1", "claim-1", "GitHub has not confirmed task completion"),
    ]
    assert not any(
        event.get("event_type") == "completion_confirmed" for event in lifecycle.events
    )


def test_github_service_client_authenticates_and_builds_bounded_pr_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "pull_request_id": "9001",
                "repository": "example/project",
                "number": 17,
            },
        )

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        operation = build_publish_pr_operation(client)
        result = operation(
            {
                "task_id": "task-1",
                "change": {
                    "pull_request_title": "A task",
                    "pull_request_body": "Risk summary",
                    "head_branch": "automation/task-1",
                    "base_branch": "main",
                },
            }
        )
    finally:
        client.close()

    assert result["pull_request_id"] == "9001"
    assert requests[0].url.path == "/internal/github/publish-pr"
    assert requests[0].headers["Authorization"] == "Bearer test-token"
