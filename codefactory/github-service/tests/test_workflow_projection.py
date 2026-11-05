import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from github_service.bootstrap import create_app
from github_service.settings import RuntimeSettings

WORKFLOW_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "openapi"
    / "github-service.json"
)
SERVICE_TOKEN = "portal-internal-service-token"


def _create_app(tmp_path):
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
        internal_service_token=SERVICE_TOKEN,
    )
    return create_app(normalize_event=lambda *_: None, settings=settings)


def _workflow_summary_schema():
    contract = json.loads(WORKFLOW_SCHEMA_PATH.read_text())
    return contract["components"]["schemas"]["WorkflowSummary"]


def test_internal_workflow_projection_exposes_safe_summary_only(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app) as client:
        store = app.state.internal_runtime.store
        store.claim_delivery("dashboard-delivery")
        event_ids = store.enqueue_events(
            "dashboard-delivery",
            [
                {
                    "task_id": "PVTI_safe-task-id",
                    "task_status": "ready",
                    "event_type": "ready",
                    "target_repositories": ["example/project"],
                    "project_owner": "example",
                    "project_owner_type": "organization",
                    "project_node_id": "PVT_project-id",
                    "project_item_id": "PVTI_safe-task-id",
                    "installation_id": 654,
                    "issue": {
                        "number": 23,
                        "body": "private-issue-body-marker",
                    },
                }
            ],
        )
        store.complete_delivery("dashboard-delivery", {"status": "queued"})
        store.register_pull_request(
            "example/project", 47, "PVTI_safe-task-id", "PR_private-id"
        )
        claim = store.claim_event()
        assert claim is not None
        assert store.retry_event(
            event_ids[0],
            str(claim["claim_token"]),
            "private-error-detail-marker",
            max_attempts=1,
        )

        response = client.get(
            "/internal/management/workflows",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
        unauthorized = client.get("/internal/management/workflows")

    assert response.status_code == 200
    assert unauthorized.status_code == 401
    workflows = response.json()["workflows"]
    Draft202012Validator(_workflow_summary_schema()).validate(workflows[0])
    assert workflows == [
        {
            "task_id": "PVTI_safe-task-id",
            "issue_number": None,
            "repository": "example/project",
            "pull_request_number": 47,
            "pull_request_id": "PR_private-id",
            "latest_event": "ready",
            "latest_event_at": workflows[0]["latest_event_at"],
            "delivery_id": "dashboard-delivery",
            "delivery_status": "accepted",
            "status": "failed",
            "attempts": 1,
            "has_failure": True,
            "pull_request_merged": None,
            "ci_status": "unknown",
            "ci_complete": None,
        }
    ]
    assert "private-issue-body-marker" not in response.text
    assert "private-error-detail-marker" not in response.text


def test_internal_workflow_projection_includes_observed_ci_and_merge_status(
    tmp_path,
):
    app = _create_app(tmp_path)

    with TestClient(app) as client:
        store = app.state.internal_runtime.store
        delivery_id = "workflow-status-delivery"
        store.claim_delivery(delivery_id)
        store.enqueue_events(
            delivery_id,
            [
                {
                    "task_id": "PVTI_status-task",
                    "task_status": "ready",
                    "event_type": "ready",
                    "target_repositories": ["example/project"],
                    "project_owner": "example",
                    "project_owner_type": "organization",
                    "project_node_id": "PVT_project-id",
                    "project_item_id": "PVTI_status-task",
                    "installation_id": 654,
                },
                {
                    "task_id": "PVTI_status-task",
                    "event_type": "ci",
                    "event_pull_request_id": "PR_status-id",
                    "ci_checks": [{"name": "tests", "status": "success"}],
                    "ci_complete": True,
                },
                {
                    "task_id": "PVTI_status-task",
                    "event_type": "merged",
                    "event_pull_request_id": "PR_status-id",
                    "merged": True,
                },
            ],
        )
        store.complete_delivery(delivery_id, {"status": "queued"})
        store.register_pull_request(
            "example/project", 48, "PVTI_status-task", "PR_status-id"
        )
        response = client.get(
            "/internal/management/workflows",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    Draft202012Validator(_workflow_summary_schema()).validate(workflow)
    assert workflow["latest_event"] == "merged"
    assert workflow["delivery_id"] == delivery_id
    assert workflow["delivery_status"] == "accepted"
    assert workflow["pull_request_merged"] is True
    assert workflow["ci_status"] == "success"
    assert workflow["ci_complete"] is True


def test_internal_workflow_projection_includes_review_changes_requested(tmp_path):
    app = _create_app(tmp_path)
    delivery_id = "review-changes-delivery"

    with TestClient(app) as client:
        store = app.state.internal_runtime.store
        store.claim_delivery(delivery_id)
        store.enqueue_events(
            delivery_id,
            [
                {
                    "task_id": "PVTI_review-task",
                    "event_type": "review_changes_requested",
                    "event_pull_request_id": "PR_review-id",
                    "review_feedback": "Please address the requested changes.",
                    "installation_id": 654,
                    "webhook_delivery_id": delivery_id,
                }
            ],
        )
        store.complete_delivery(delivery_id, {"status": "queued"})
        response = client.get(
            "/internal/management/workflows",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    Draft202012Validator(_workflow_summary_schema()).validate(workflow)
    assert workflow["latest_event"] == "review_changes_requested"


def test_internal_workflow_projection_keeps_empty_ci_unavailable(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app) as client:
        store = app.state.internal_runtime.store
        delivery_id = "no-ci-delivery"
        store.claim_delivery(delivery_id)
        store.enqueue_events(
            delivery_id,
            [
                {
                    "task_id": "PVTI_no-ci-task",
                    "task_status": "ready",
                    "event_type": "ready",
                    "target_repositories": ["example/project"],
                    "project_owner": "example",
                    "project_owner_type": "organization",
                    "project_node_id": "PVT_project-id",
                    "project_item_id": "PVTI_no-ci-task",
                    "installation_id": 654,
                },
                {
                    "task_id": "PVTI_no-ci-task",
                    "event_type": "ci",
                    "event_pull_request_id": "PR_no-ci-id",
                    "ci_checks": [],
                    "ci_complete": True,
                },
            ],
        )
        store.complete_delivery(delivery_id, {"status": "queued"})
        store.register_pull_request(
            "example/project", 49, "PVTI_no-ci-task", "PR_no-ci-id"
        )
        response = client.get(
            "/internal/management/workflows",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )

    workflow = response.json()["workflows"][0]
    Draft202012Validator(_workflow_summary_schema()).validate(workflow)
    assert workflow["ci_status"] == "unavailable"
    assert workflow["ci_complete"] is True
