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
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
        internal_service_token="service-token",
    )
    app = create_app(
        normalize_event=lambda *_: None,
        settings=settings,
        complete_task_factory=lambda _store: (
            lambda task_id: {"completed": task_id == "task-1"}
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
                "read_files": lambda _self, task_id, paths: {
                    "files": [{"path": paths[0], "content": "print('task')\n"}]
                },
            },
        )(),
    )
    with TestClient(app) as client:
        unauthorized = client.post(
            "/internal/github/complete-task",
            json={"task_id": "task-1"},
        )
        assert unauthorized.status_code == 401
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

        headers = {"authorization": "Bearer service-token"}
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
