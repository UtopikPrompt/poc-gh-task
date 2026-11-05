import hashlib
import hmac
import json
import logging

from fastapi.testclient import TestClient
from github_service.bootstrap import create_app
from github_service.events import (
    GitHubEventNormalizer,
    GitHubWebhook,
    ProjectEventConfig,
)
from github_service.settings import RuntimeSettings


def _signature(body, secret):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_internal_api_requires_bearer_auth_and_claims_durable_events(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
        internal_service_token="service-token",
    )

    app = create_app(
        normalize_event=lambda *_: {
            "task_id": "task-1",
            "event_type": "ready",
            "project_owner": "example-org",
            "project_owner_type": "organization",
            "project_node_id": "PVT_project",
            "project_item_id": "PVTI_item",
            "target_repositories": ["example/project"],
            "installation_id": 654,
        },
        settings=settings,
    )
    with TestClient(app) as client:
        unauthorized = client.post("/internal/events/claim")
        assert unauthorized.status_code == 401

        body = json.dumps(
            {
                "action": "edited",
                "installation": {"id": 654},
                "title": "payload-title-marker",
                "body": "payload-body-marker",
                "token": "payload-token-marker",
            }
        ).encode()
        accepted = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "projects_v2_item",
                "x-github-delivery": "delivery-1",
                "x-hub-signature-256": _signature(body, "webhook-secret"),
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "queued"
        decision = next(
            record
            for record in caplog.records
            if record.getMessage() == "GitHub webhook decision"
        )
        assert decision.github_event == "projects_v2_item"
        assert decision.github_action == "edited"
        assert decision.reason == "accepted"
        assert "webhook-secret" not in caplog.text
        assert "payload-title-marker" not in caplog.text
        assert "payload-body-marker" not in caplog.text
        assert "payload-token-marker" not in caplog.text

        headers = {"authorization": "Bearer service-token"}
        claim_response = client.post("/internal/events/claim", headers=headers)
        claim = claim_response.json()["event"]
        assert claim["payload"]["task_id"] == "task-1"
        ack = client.post(
            f"/internal/events/{claim['event_id']}/ack",
            headers=headers,
            json={"claim_token": claim["claim_token"]},
        )
        assert ack.status_code == 200


def test_internal_github_capabilities_require_authentication_and_validate_request(
    tmp_path,
):
    completion_calls = []
    feedback_calls = []
    source_file_calls = []
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
        internal_service_token="service-token",
    )

    def read_source_files(
        _self, task_id, paths, pull_request_number=None, expected_pull_request_id=None
    ):
        source_file_calls.append(
            (task_id, paths, pull_request_number, expected_pull_request_id)
        )
        return {"files": [{"path": paths[0], "content": "print('task')\n"}]}

    app = create_app(
        normalize_event=lambda *_: None,
        settings=settings,
        complete_task_factory=lambda _store: (
            lambda task_id, resolution=None: (
                completion_calls.append((task_id, resolution))
                or {"completed": task_id == "task-1"}
            )
        ),
        start_task_factory=lambda _store: (
            lambda task_id: {
                "started": task_id == "task-1",
                **(
                    {"project_update": {"updated": True}} if task_id == "task-1" else {}
                ),
            }
        ),
        propose_file_changes_factory=lambda _store: (
            lambda proposal: {
                "pull_request_id": "9001",
                "repository": "example/project",
                "number": 17,
                "received_task_id": proposal.task_id,
                "received_summary": proposal.summary,
                "received_risk_summary": proposal.risk_summary,
                "received_changes": [
                    {
                        "operation": change.operation,
                        "path": change.path,
                        "content": change.content,
                    }
                    for change in proposal.changes
                ],
            }
        ),
        source_context_factory=lambda _store: type(
            "FakeSource",
            (),
            {
                "candidate_paths": lambda _self, task_id: {
                    "paths": [{"path": "src/task.py", "size": 12}]
                },
                "read_files": read_source_files,
            },
        )(),
        request_human_feedback_factory=lambda _store: (
            lambda task_id, question, dedupe_key, source_comment_id: (
                feedback_calls.append(
                    (task_id, question, dedupe_key, source_comment_id)
                )
                or {"comment_id": "comment-17", "created": True}
            )
        ),
    )
    with TestClient(app) as client:
        unauthorized = client.post(
            "/internal/github/complete-task",
            json={"task_id": "task-1"},
        )
        assert unauthorized.status_code == 401
        unauthorized_start = client.post(
            "/internal/github/start-task",
            json={"task_id": "task-1"},
        )
        assert unauthorized_start.status_code == 401
        unauthorized_proposal = client.post(
            "/internal/github/propose-changes",
            json={"task_id": "task-1", "changes": []},
        )
        assert unauthorized_proposal.status_code == 401
        unauthorized_source = client.post(
            "/internal/github/source-tree",
            json={"task_id": "task-1"},
        )
        assert unauthorized_source.status_code == 401
        unauthorized_feedback = client.post(
            "/internal/github/request-human-feedback",
            json={
                "task_id": "task-1",
                "question": "Clarify this?",
                "dedupe_key": "a" * 64,
            },
        )
        assert unauthorized_feedback.status_code == 401

        headers = {"authorization": "Bearer service-token"}
        feedback = client.post(
            "/internal/github/request-human-feedback",
            headers=headers,
            json={
                "task_id": "task-1",
                "question": "Should archived records be included?",
                "dedupe_key": "a" * 64,
            },
        )
        assert feedback.status_code == 200
        assert feedback.json() == {"comment_id": "comment-17", "created": True}
        assert feedback_calls == [
            ("task-1", "Should archived records be included?", "a" * 64, None)
        ]
        inline_feedback = client.post(
            "/internal/github/request-human-feedback",
            headers=headers,
            json={
                "task_id": "task-1",
                "question": "Should this README change be reverted?",
                "dedupe_key": "b" * 64,
                "source_review_comment_id": 4461111142,
            },
        )
        assert inline_feedback.status_code == 200
        assert feedback_calls[-1] == (
            "task-1",
            "Should this README change be reverted?",
            "b" * 64,
            4461111142,
        )
        malformed_feedback = client.post(
            "/internal/github/request-human-feedback",
            headers=headers,
            json={"task_id": "task-1", "question": "Clarify this?"},
        )
        assert malformed_feedback.status_code == 400

        store = client.app.state.internal_runtime.store
        store.claim_delivery("retry-delivery")
        retry_event_id = store.enqueue_events(
            "retry-delivery", [{"task_id": "task-dead", "event_type": "ci"}]
        )[0]
        store.complete_delivery("retry-delivery", {"status": "queued"})
        dead_claim = store.claim_event(max_attempts=1)
        assert dead_claim is not None
        assert store.retry_event(
            retry_event_id,
            dead_claim["claim_token"],
            "private failure detail",
            max_attempts=1,
        )
        unauthorized_retry = client.post(
            "/internal/management/workflows/task-dead/retry"
        )
        assert unauthorized_retry.status_code == 401
        requeued = client.post(
            "/internal/management/workflows/task-dead/retry",
            headers=headers,
        )
        assert requeued.status_code == 200
        assert requeued.json() == {"requeued": True}
        second_retry = client.post(
            "/internal/management/workflows/task-dead/retry",
            headers=headers,
        )
        assert second_retry.status_code == 409

        source_tree = client.post(
            "/internal/github/source-tree",
            headers=headers,
            json={"task_id": "task-1"},
        )
        assert source_tree.status_code == 200
        assert source_tree.json() == {"paths": [{"path": "src/task.py", "size": 12}]}
        source_files = client.post(
            "/internal/github/source-files",
            headers=headers,
            json={"task_id": "task-1", "paths": ["src/task.py"]},
        )
        assert source_files.status_code == 200
        assert source_files.json() == {
            "files": [{"path": "src/task.py", "content": "print('task')\n"}]
        }
        pull_request_source_files = client.post(
            "/internal/github/source-files",
            headers=headers,
            json={
                "task_id": "task-1",
                "paths": ["src/task.py"],
                "pull_request_number": 17,
                "expected_pull_request_id": "9001",
            },
        )
        assert pull_request_source_files.status_code == 200
        assert source_file_calls[-1] == ("task-1", ["src/task.py"], 17, "9001")
        invalid_source_request = client.post(
            "/internal/github/source-files",
            headers=headers,
            json={
                "task_id": "task-1",
                "paths": ["src/task.py"],
                "repository": "attacker/repo",
            },
        )
        assert invalid_source_request.status_code == 400

        incomplete = client.post(
            "/internal/github/propose-changes",
            headers=headers,
            json={"task_id": "task-1", "changes": []},
        )
        assert incomplete.status_code == 400

        published = client.post(
            "/internal/github/propose-changes",
            headers=headers,
            json={
                "task_id": "task-1",
                "summary": "Implement task behavior.",
                "risk_summary": "Review potential caller impact.",
                "changes": [
                    {
                        "operation": "create",
                        "path": "src/task.py",
                        "content": "print('task')\n",
                    }
                ],
            },
        )
        assert published.status_code == 200
        assert published.json()["received_task_id"] == "task-1"
        assert published.json()["received_summary"] == "Implement task behavior."
        assert (
            published.json()["received_risk_summary"]
            == "Review potential caller impact."
        )
        assert published.json()["received_changes"][0]["path"] == "src/task.py"

        rejected = client.post(
            "/internal/github/propose-changes",
            headers=headers,
            json={
                "task_id": "task-1",
                "changes": [{"operation": "delete", "path": "src/a.py", "content": ""}],
            },
        )
        assert rejected.status_code == 400

        too_large = client.post(
            "/internal/github/propose-changes",
            headers=headers,
            content=b"x" * (2 * 1024 * 1024 + 1),
        )
        assert too_large.status_code == 413

        completed = client.post(
            "/internal/github/complete-task",
            headers=headers,
            json={"task_id": "task-1"},
        )
        assert completed.status_code == 200
        assert completed.json() == {"completed": True}
        resolution = {
            "actor": "verified-owner",
            "decided_at": "2026-11-03T12:00:00+00:00",
            "decision": "proceed_without_ci",
            "pull_request_id": "PR-17",
        }
        completed_without_ci = client.post(
            "/internal/github/complete-task",
            headers=headers,
            json={"task_id": "task-1", "no_ci_resolution": resolution},
        )
        assert completed_without_ci.status_code == 200
        assert completion_calls == [
            ("task-1", None),
            ("task-1", resolution),
        ]
        invalid_completion = client.post(
            "/internal/github/complete-task",
            headers=headers,
            json={"task_id": "task-1", "unexpected": True},
        )
        assert invalid_completion.status_code == 400

        started = client.post(
            "/internal/github/start-task",
            headers=headers,
            json={"task_id": "task-1"},
        )
        assert started.status_code == 200
        assert started.json() == {
            "started": True,
            "project_update": {"updated": True},
        }
        rejected_start = client.post(
            "/internal/github/start-task",
            headers=headers,
            json={"task_id": "task-2"},
        )
        assert rejected_start.status_code == 409


def test_normalized_event_cannot_substitute_webhook_installation(tmp_path):
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
    )
    app = create_app(
        normalize_event=lambda *_: {
            "task_id": "task-1",
            "event_type": "ci",
            "installation_id": 987,
        },
        settings=settings,
    )
    body = json.dumps({"installation": {"id": 654}}).encode()
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "check_run",
                "x-github-delivery": "delivery-mismatch",
                "x-hub-signature-256": _signature(body, "webhook-secret"),
            },
        )

    assert response.status_code == 400
    assert "does not match webhook" in response.json()["detail"]


def test_normalizer_logs_status_rejection_without_payload_content(caplog):
    class FakeAPI:
        def get_project_item(self, _item_node_id, _installation_id):
            return {
                "status_field_name": "Status",
                "status_option_name": "In Progress",
            }

    class FakeStore:
        def task_context(self, _task_id):
            return None

    webhook = GitHubWebhook(
        event_name="projects_v2_item",
        delivery_id="delivery-ignored",
        installation_id=654,
        payload={
            "action": "edited",
            "installation": {"id": 654},
            "projects_v2_item": {"node_id": "PVTI_private-item"},
            "body": "payload-body-marker",
        },
    )
    caplog.set_level(logging.INFO)

    result = GitHubEventNormalizer(FakeAPI(), ProjectEventConfig())(
        webhook, FakeStore()
    )

    assert result is None
    decision = next(
        record
        for record in caplog.records
        if record.getMessage() == "GitHub webhook decision"
    )
    assert decision.github_event == "projects_v2_item"
    assert decision.github_action == "edited"
    assert decision.reason == "status_mismatch"
    assert "payload-body-marker" not in caplog.text
    assert "PVTI_private-item" not in caplog.text
