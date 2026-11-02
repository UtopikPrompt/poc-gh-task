import json
import logging

import httpx
import pytest
import langgraph_service.github_client as github_client
from langgraph_service.github_client import (
    GitHubServiceClient,
    build_submit_proposal_operation,
)
from langgraph_service.worker import LangGraphEventWorker


def test_claim_event_retries_connect_timeout_and_recovers(caplog, monkeypatch):
    requests = []
    delays = []
    event = {"event_id": "event-1", "claim_token": "claim-1"}

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(200, json={"event": event})

    monkeypatch.setattr(github_client.time, "sleep", delays.append)
    caplog.set_level(logging.WARNING, logger="langgraph_service.github_client")
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.claim_event() == event
    finally:
        client.close()

    assert len(requests) == 3
    assert [request.url.path for request in requests] == [
        "/internal/events/claim",
        "/internal/events/claim",
        "/internal/events/claim",
    ]
    assert delays == [0.25, 0.5]
    assert caplog.text.count("retrying attempt") == 2


def test_claim_event_stops_after_bounded_connect_timeout_attempts(caplog, monkeypatch):
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectTimeout("connect timed out", request=request)

    monkeypatch.setattr(github_client.time, "sleep", delays.append)
    caplog.set_level(logging.WARNING, logger="langgraph_service.github_client")
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ConnectTimeout, match="connect timed out"):
            client.claim_event()
    finally:
        client.close()

    assert len(requests) == github_client.CLAIM_MAX_ATTEMPTS
    assert delays == [0.25, 0.5]
    assert "failed after 3 connection attempts" in caplog.text


def test_claim_event_does_not_retry_read_timeout_after_possible_server_commit(
    monkeypatch,
):
    server_claims = []
    delays = []

    def handler(request):
        server_claims.append(request)
        raise httpx.ReadTimeout("response timed out", request=request)

    monkeypatch.setattr(github_client.time, "sleep", delays.append)
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ReadTimeout, match="response timed out"):
            client.claim_event()
    finally:
        client.close()

    assert len(server_claims) == 1
    assert delays == []


def test_claim_event_does_not_retry_non_success_http_response(monkeypatch):
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"detail": "unavailable"})

    monkeypatch.setattr(github_client.time, "sleep", delays.append)
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.claim_event()
    finally:
        client.close()

    assert len(requests) == 1
    assert delays == []


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


def test_worker_does_not_claim_events_when_local_model_is_unavailable():
    queue = FakeQueue()
    worker = LangGraphEventWorker(
        queue=queue,
        lifecycle=FakeLifecycle(),
        readiness=lambda: False,
    )

    assert worker.process_one() is False
    assert queue.calls == []
    assert queue.event is not None


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


def test_github_service_client_authenticates_and_submits_typed_file_changes():
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
        operation = build_submit_proposal_operation(client)
        result = operation(
            {
                "task_id": "task-1",
                "change": {
                    "summary": "prepared",
                    "risk_summary": "Review potential caller impact.",
                    "changes": [
                        {
                            "operation": "create",
                            "path": "src/task.py",
                            "content": "print('task')\n",
                        }
                    ],
                },
            }
        )
    finally:
        client.close()

    assert result["pull_request_id"] == "9001"
    assert requests[0].url.path == "/internal/github/propose-changes"
    assert requests[0].headers["Authorization"] == "Bearer test-token"
    assert json.loads(requests[0].content) == {
        "task_id": "task-1",
        "summary": "prepared",
        "risk_summary": "Review potential caller impact.",
        "changes": [
            {
                "operation": "create",
                "path": "src/task.py",
                "content": "print('task')\n",
            }
        ],
    }


def test_proposal_operation_rejects_old_metadata_and_invalid_operations():
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    try:
        operation = build_submit_proposal_operation(client)
        with pytest.raises(ValueError, match="no file changes"):
            operation({"task_id": "task-1", "change": {"head_branch": "main"}})
        with pytest.raises(ValueError, match="invalid file change"):
            operation(
                {
                    "task_id": "task-1",
                    "change": {
                        "changes": [
                            {"operation": "delete", "path": "a.py", "content": ""}
                        ]
                    },
                }
            )
    finally:
        client.close()
