import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from github_service.bootstrap import create_app
from github_service.settings import RuntimeSettings


def _signature(body, secret):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_internal_api_requires_bearer_auth_and_claims_durable_events(tmp_path):
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
        internal_service_token="service-token",
    )
    app = create_app(
        normalize_event=lambda *_: {
            "task_id": "task-1",
            "event_type": "ready",
            "target_repositories": ["example/project"],
        },
        settings=settings,
    )
    with TestClient(app) as client:
        unauthorized = client.post("/internal/events/claim")
        assert unauthorized.status_code == 401

        body = json.dumps({"action": "edited"}).encode()
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
        publish_pull_request_factory=lambda _store: (
            lambda **request: {
                "pull_request_id": "9001",
                "repository": "example/project",
                "number": 17,
                "received": request,
            }
        ),
    )
    with TestClient(app) as client:
        unauthorized = client.post(
            "/internal/github/complete-task",
            json={"task_id": "task-1"},
        )
        assert unauthorized.status_code == 401

        headers = {"authorization": "Bearer service-token"}
        incomplete = client.post(
            "/internal/github/publish-pr",
            headers=headers,
            json={"task_id": "task-1", "title": "missing fields"},
        )
        assert incomplete.status_code == 400

        published = client.post(
            "/internal/github/publish-pr",
            headers=headers,
            json={
                "task_id": "task-1",
                "title": "Task PR",
                "body": "Risk summary",
                "head": "automation/task-1",
                "base": "main",
            },
        )
        assert published.status_code == 200
        assert published.json()["received"]["task_id"] == "task-1"

        completed = client.post(
            "/internal/github/complete-task",
            headers=headers,
            json={"task_id": "task-1"},
        )
        assert completed.status_code == 200
        assert completed.json() == {"completed": True}
