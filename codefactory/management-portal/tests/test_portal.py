import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from jsonschema import Draft202012Validator
from fastapi.testclient import TestClient
from management_portal.auth import (
    DASHBOARD_STATE_COOKIE,
    DASHBOARD_SESSION_SECONDS,
    DASHBOARD_STATE_SECONDS,
    GITHUB_ORGANIZATION,
)
from management_portal.host import create_app
from management_portal.preview import (
    _is_loopback_client,
    create_app as create_preview_app,
)
from management_portal.settings import GITHUB_OAUTH_CALLBACK_URL, PortalSettings

OAUTH_CLIENT_SECRET = "oauth-client-secret-marker"
OAUTH_ACCESS_TOKEN = "oauth-access-token-marker"
SESSION_SIGNING_KEY = "dashboard-session-signing-key-test-value"
WORKFLOW_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "openapi"
    / "management-portal.json"
)


def _create_app(tmp_path, *, oauth=True):
    settings = PortalSettings(
        github_oauth_client_id="oauth-client-id" if oauth else None,
        github_oauth_client_secret=OAUTH_CLIENT_SECRET if oauth else None,
        dashboard_session_signing_key=SESSION_SIGNING_KEY if oauth else None,
        internal_service_token="portal-internal-service-token",
    )
    app = create_app(settings=settings)
    app.state.oauth_http_transport = httpx.MockTransport(_oauth_response)
    app.state.github_http_transport = httpx.MockTransport(_github_response)
    app.state.langgraph_http_transport = httpx.MockTransport(_langgraph_response)
    return app


def _workflow_summary(**overrides):
    summary = {
        "task_id": "PVTI_safe-task-id",
        "issue_number": None,
        "repository": "example/project",
        "pull_request_number": 47,
        "pull_request_id": "PR_safe-id",
        "latest_event": "ready",
        "latest_event_at": "2026-11-02T12:00:00Z",
        "delivery_id": "dashboard-delivery",
        "delivery_status": "accepted",
        "status": "processed",
        "attempts": 0,
        "has_failure": False,
        "pull_request_merged": None,
        "ci_status": "unknown",
        "ci_complete": None,
    }
    return {**summary, **overrides}


def _langgraph_response(request):
    if request.url.path == "/internal/management/executions":
        return httpx.Response(200, json={"executions": []})
    return httpx.Response(
        200,
        json={
            "config": {
                "completion_timeout_seconds": 45,
            },
            "version": 0,
            "updated_at": None,
            "updated_by": None,
        },
    )


def _github_response(request):
    assert request.headers["Authorization"] == ("Bearer portal-internal-service-token")
    if request.url.path == "/internal/management/workflows":
        return httpx.Response(200, json={"workflows": []})
    return httpx.Response(404)


def _oauth_response(request):
    if request.url == "https://github.com/login/oauth/access_token":
        assert request.content.decode().find("code=authorization-code") >= 0
        return httpx.Response(
            200,
            json={"access_token": OAUTH_ACCESS_TOKEN, "scope": "read:org"},
        )
    if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
        return httpx.Response(
            200,
            json={
                "state": "active",
                "role": "member",
                "organization": {"login": GITHUB_ORGANIZATION},
            },
        )
    if request.url.path == "/user":
        return httpx.Response(200, json={"login": "approved-member"})
    return httpx.Response(404)


def _login(client):
    login_response = client.get("/auth/login", follow_redirects=False)
    authorization = urlsplit(login_response.headers["location"])
    authorization_query = parse_qs(authorization.query)
    assert authorization.scheme == "https"
    assert authorization.netloc == "github.com"
    assert authorization_query["redirect_uri"] == [GITHUB_OAUTH_CALLBACK_URL]
    assert authorization_query["scope"] == ["read:org"]
    callback_response = client.get(
        "/auth",
        params={"code": "authorization-code", "state": authorization_query["state"][0]},
        follow_redirects=False,
    )
    return login_response, callback_response


def _login_as_owner(client, app):
    def owner_oauth_response(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "role": "admin",
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "verified-owner"})
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(owner_oauth_response)
    return _login(client)


def _roster_payload():
    profiles = [
        ("context", "gather_context"),
        ("planning", "create_plan"),
        ("risk", "assess_risk"),
        ("implementation", "prepare_changes"),
        ("repair", "repair_changes"),
    ]
    agents = [
        {
            "agent_id": role,
            "name": role.title(),
            "description": f"{role} profile",
            "prompt_content": "",
            "model_profile": "local_llama_cpp",
            "skill_ids": ["review_notes"] if role == "context" else [],
            "tool_profile": tool_profile,
        }
        for role, tool_profile in profiles
    ]
    return {
        "agents": agents,
        "assignments": {role: role for role, _ in profiles},
    }


def _set_cookie(response, name):
    return next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{name}=")
    )


def test_workflow_dashboard_api_exposes_safe_summary_only(tmp_path):
    app = _create_app(tmp_path)
    private_summary = _workflow_summary(
        status="failed",
        attempts=1,
        has_failure=True,
        delivery_id="dashboard-delivery",
    )
    github_requests = []

    def github_response(request):
        github_requests.append(request)
        return httpx.Response(200, json={"workflows": [private_summary]})

    app.state.github_http_transport = httpx.MockTransport(github_response)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
        response = client.get("/api/workflows")

    assert response.status_code == 200
    assert len(github_requests) == 1
    assert github_requests[0].headers["Authorization"] == (
        "Bearer portal-internal-service-token"
    )
    workflows = response.json()["workflows"]
    contract = json.loads(WORKFLOW_SCHEMA_PATH.read_text())
    summary_schema = contract["components"]["schemas"]["WorkflowSummary"]
    Draft202012Validator(summary_schema).validate(workflows[0])
    assert workflows == [
        {**private_summary, "workflow_status": None, "waiting_for": None}
    ]
    assert "private-issue-body-marker" not in response.text
    assert "private-error-detail-marker" not in response.text
    assert OAUTH_CLIENT_SECRET not in response.text
    assert OAUTH_ACCESS_TOKEN not in response.text


def test_workflow_dashboard_reports_observed_ci_and_merge_status(tmp_path):
    app = _create_app(tmp_path)
    summary = _workflow_summary(
        latest_event="merged",
        delivery_id="workflow-status-delivery",
        pull_request_merged=True,
        ci_status="success",
        ci_complete=True,
    )
    app.state.github_http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"workflows": [summary]})
    )

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
        response = client.get("/api/workflows")

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    contract = json.loads(WORKFLOW_SCHEMA_PATH.read_text())
    summary_schema = contract["components"]["schemas"]["WorkflowSummary"]
    Draft202012Validator(summary_schema).validate(workflow)
    assert workflow["latest_event"] == "merged"
    assert workflow["delivery_id"] == "workflow-status-delivery"
    assert workflow["delivery_status"] == "accepted"
    assert workflow["pull_request_merged"] is True
    assert workflow["ci_status"] == "success"
    assert workflow["ci_complete"] is True


def test_review_changes_requested_workflow_is_projected_and_labeled(tmp_path):
    app = _create_app(tmp_path)
    summary = _workflow_summary(latest_event="review_changes_requested")
    app.state.github_http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"workflows": [summary]})
    )

    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        response = client.get("/api/workflows")
        script = client.get("/management-portal/assets/portal.js")

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    contract = json.loads(WORKFLOW_SCHEMA_PATH.read_text())
    summary_schema = contract["components"]["schemas"]["WorkflowSummary"]
    Draft202012Validator(summary_schema).validate(workflow)
    assert workflow["latest_event"] == "review_changes_requested"
    assert "review_changes_requested: 'Review changes requested'" in script.text
    assert "repair_unresolved: 'Needs reviewer feedback'" in script.text
    assert "human_feedback: 'human feedback'" in script.text
    assert "readableState(workflow.latest_event, latestEventLabels)" in script.text


def test_workflow_dashboard_projects_lifecycle_separately_from_queue_status(tmp_path):
    app = _create_app(tmp_path)
    summary = _workflow_summary(
        task_id="PVTI_lADOE2vr3s4BpgD7zhFiGco",
        latest_event="review_changes_requested",
        status="processed",
    )
    app.state.github_http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"workflows": [summary]})
    )
    execution_requests = []

    def langgraph_response(request):
        if request.url.path == "/internal/management/executions":
            execution_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "executions": [
                        {
                            "thread_id": summary["task_id"],
                            "workflow_status": "repair_unresolved",
                            "waiting_for": "human_feedback",
                        }
                    ]
                },
            )
        return _langgraph_response(request)

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)

    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        response = client.get("/api/workflows")
        dashboard = client.get("/")
        script = client.get("/management-portal/assets/portal.js")

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    assert workflow["workflow_status"] == "repair_unresolved"
    assert workflow["waiting_for"] == "human_feedback"
    assert workflow["status"] == "processed"
    assert len(execution_requests) == 1
    assert execution_requests[0].url.params["limit"] == "100"
    assert "<th>Workflow status</th>" in dashboard.text
    assert "<th>Waiting for</th>" in dashboard.text
    assert "<th>Queue status</th>" in dashboard.text
    assert "repair_unresolved: 'Needs reviewer feedback'" in script.text
    assert "human_feedback: 'human feedback'" in script.text
    assert "const queueStatus = cell(row," in script.text
    assert "workflow.workflow_status === 'repair_unresolved'" in script.text
    assert (
        "if (workflow.workflow_status === 'repair_unresolved') appendReviewLink(lifecycleStatus, workflow);"
        in script.text
    )
    assert (
        "pullRequestUrl(workflow.repository, workflow.pull_request_number)"
        in script.text
    )
    assert "function appendReviewLink(target, workflow)" in script.text
    link_helper = script.text.split("function appendReviewLink(target, workflow) {", 1)[
        1
    ].split("\n}", 1)[0]
    assert "workflow.workflow_status" not in link_helper
    assert "appendReviewLink(lifecycleStatus, workflow)" in script.text
    assert (
        "appendReviewLink(executionStatus, workflowsByTaskId.get(execution.thread_id))"
        in script.text
    )
    assert "reviewLink.textContent = 'Review feedback'" in script.text
    assert "reviewLink.href = reviewUrl" in script.text
    assert (
        "https://github.com/${repositoryParts.map(encodeURIComponent).join('/')}/pull/${pullRequestNumber}"
        in script.text
    )


def test_execution_flow_distinguishes_unresolved_repair_from_merge_wait(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        script = client.get("/management-portal/assets/portal.js")

    assert script.status_code == 200
    assert "human_feedback: 'repair'" in script.text
    assert "typeof checkpoint.event_type === 'string'" in script.text
    assert "checkpoint.event_type === newerCheckpoint.event_type" in script.text
    assert "newerCheckpoint.step > checkpoint.step" in script.text
    assert "latest.workflow_status === 'repair_unresolved'" in script.text
    assert "latest.waiting_for === 'human_feedback'" in script.text
    assert "status = 'Needs reviewer feedback'" in script.text
    assert (
        "appendReviewLink(nodeStatus, workflowsByTaskId.get(detail.thread_id))"
        in script.text
    )
    assert "Clarify the requested changes in a new review to continue." in script.text
    assert "human_merge: 'human_merge_boundary'" in script.text
    assert script.text.index("else if (waitingNode === nodeId)") < script.text.index(
        "else if (completedNodes.has(nodeId))"
    )
    assert "awaiting_human_merge: 'Waiting for a human merge'" in script.text


def test_execution_flow_shows_completed_stages_and_current_phase_only(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        script = client.get("/management-portal/assets/portal.js")

    assert script.status_code == 200
    assert "status = 'Complete'" in script.text
    assert (
        "if (!['complete', 'waiting', 'stopped'].includes(statusClass)) continue;"
        in script.text
    )
    assert "if (visibleNodeIds.has(nodeId)) continue;" in script.text
    assert "if (!list.children.length) continue;" in script.text
    assert "Not reached" not in script.text
    assert "Up next" not in script.text


def test_workflow_dashboard_caps_workflow_summaries_at_100(tmp_path):
    app = _create_app(tmp_path)
    summaries = [_workflow_summary(task_id=f"task-{index}") for index in range(101)]
    app.state.github_http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"workflows": summaries})
    )

    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        response = client.get("/api/workflows")

    assert response.status_code == 200
    workflows = response.json()["workflows"]
    assert len(workflows) == 100
    assert workflows[0]["task_id"] == "task-0"
    assert workflows[-1]["task_id"] == "task-99"


def test_workflow_retry_requires_owner_and_same_origin(tmp_path):
    app = _create_app(tmp_path)
    github_requests = []

    def github_response(request):
        github_requests.append(request)
        return httpx.Response(200, json={"requeued": True})

    app.state.github_http_transport = httpx.MockTransport(github_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        member_retry = client.post(
            "/api/workflows/PVTI_safe-task-id/retry",
            headers={"origin": "https://testserver"},
        )

    assert member_retry.status_code == 403
    assert github_requests == []

    def owner_oauth_response(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "role": "admin",
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "verified-owner"})
        return _oauth_response(request)

    app = _create_app(tmp_path)
    app.state.oauth_http_transport = httpx.MockTransport(owner_oauth_response)
    app.state.github_http_transport = httpx.MockTransport(github_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        cross_origin = client.post(
            "/api/workflows/PVTI_safe-task-id/retry",
            headers={"origin": "https://attacker.example"},
        )
        retried = client.post(
            "/api/workflows/PVTI_safe-task-id/retry",
            headers={"origin": "https://testserver"},
        )

    assert cross_origin.status_code == 403
    assert retried.status_code == 200
    assert retried.json() == {"requeued": True}
    assert len(github_requests) == 1
    assert github_requests[0].method == "POST"
    assert github_requests[0].url.path == (
        "/internal/management/workflows/PVTI_safe-task-id/retry"
    )
    assert github_requests[0].headers["Authorization"] == (
        "Bearer portal-internal-service-token"
    )


def test_workflow_retry_hides_upstream_failure_details(tmp_path):
    app = _create_app(tmp_path)
    sensitive_error = "private-upstream-error-marker"

    def owner_oauth_response(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "role": "admin",
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "verified-owner"})
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(owner_oauth_response)
    app.state.github_http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(409, text=sensitive_error)
    )
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        response = client.post(
            "/api/workflows/PVTI_safe-task-id/retry",
            headers={"origin": "https://testserver"},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "workflow is not eligible for retry"}
    assert sensitive_error not in response.text


def test_no_ci_resolution_requires_owner_and_same_origin(tmp_path):
    app = _create_app(tmp_path)
    forwarded = []

    def owner_oauth_response(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "role": "admin",
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "verified-owner"})
        return _oauth_response(request)

    def langgraph_response(request):
        forwarded.append(request)
        return httpx.Response(200, json={"resolved": True})

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        member = client.post(
            "/api/executions/PVTI_safe-task-id/resolve-no-ci",
            headers={"origin": "https://testserver"},
        )

    assert member.status_code == 403
    assert forwarded == []

    app = _create_app(tmp_path)
    app.state.oauth_http_transport = httpx.MockTransport(owner_oauth_response)
    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        cross_origin = client.post(
            "/api/executions/PVTI_safe-task-id/resolve-no-ci",
            headers={"origin": "https://attacker.example"},
        )
        resolved = client.post(
            "/api/executions/PVTI_safe-task-id/resolve-no-ci",
            headers={"origin": "https://testserver"},
        )

    assert cross_origin.status_code == 403
    assert resolved.status_code == 200
    assert resolved.json() == {"resolved": True}
    assert len(forwarded) == 1
    assert forwarded[0].method == "POST"
    assert forwarded[0].url.path == (
        "/internal/management/executions/PVTI_safe-task-id/resolve-no-ci"
    )
    assert forwarded[0].headers["Authorization"] == (
        "Bearer portal-internal-service-token"
    )
    assert json.loads(forwarded[0].content) == {
        "actor": "verified-owner",
        "decision": "proceed_without_ci",
    }


def test_workflow_dashboard_page_is_served_by_management_portal(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
        response = client.get("/")
        settings_page = client.get("/settings")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Codefactory workflow state" in response.text
    assert "/management-portal/assets/portal.css" in response.text
    assert "/management-portal/assets/portal.js" in response.text
    assert 'id="live-status"' in response.text
    assert "/settings" in response.text
    assert 'href="/" aria-current="page"' in response.text
    assert "agent-config-form" not in response.text
    assert settings_page.status_code == 200
    assert "Agent configuration" in settings_page.text
    assert 'href="/settings" aria-current="page"' in settings_page.text
    assert (
        settings_page.text.index('id="agent-roster-heading"')
        < settings_page.text.index('id="inference-heading"')
        < settings_page.text.index('id="agent-config-heading"')
    )
    assert "agent-config-form" in settings_page.text
    assert 'id="inference-instance-list"' in settings_page.text
    assert 'id="inference-instance-template"' in settings_page.text
    assert 'id="inference-instance-create-form"' in settings_page.text
    assert 'name="instance_id"' in settings_page.text
    assert 'class="delete-inference-instance"' in settings_page.text
    assert "A matching inference Deployment must" in settings_page.text
    assert 'id="model-download-form"' in settings_page.text
    assert 'id="model-download-instance"' not in settings_page.text
    assert "Download to shared storage" in settings_page.text
    assert "Instance to update" not in settings_page.text
    assert '<label for="model-download-filename">Save as</label>' in settings_page.text
    assert (
        '<label for="model-download-url">Hugging Face model file URL</label>'
        in settings_page.text
    )
    assert (
        'class="download-model" type="submit" disabled>Download</button>'
        in settings_page.text
    )
    assert "added to shared storage" in settings_page.text
    assert "Assign it to an instance separately with Apply model." in settings_page.text
    assert (
        settings_page.text.index("Hugging Face model file URL")
        < settings_page.text.index("Save as")
        < settings_page.text.index(">Download</button>")
    )
    assert 'id="model-catalog-status"' in settings_page.text
    assert 'id="toast-region"' in settings_page.text
    assert settings_page.text.count('class="download-model"') == 1
    assert 'name="model_filename"' in settings_page.text
    assert (
        '<select id="model-filename" class="instance-model-input" name="model_filename"'
        in settings_page.text
    )
    assert 'name="model_url"' in settings_page.text
    assert 'name="model_sha256"' not in settings_page.text
    assert 'data-instance="gpu"' not in settings_page.text
    assert "CPU / GPU" in settings_page.text
    assert "RAM / VRAM" in settings_page.text
    assert 'id="inference-metrics-body"' in settings_page.text
    assert 'id="model-profile"' not in settings_page.text
    assert "/management-portal/assets/settings.js" in settings_page.text
    assert 'id="roster-summary"' in settings_page.text
    assert 'id="add-profile-role"' in settings_page.text
    assert 'id="roster-document"' not in settings_page.text

    script = client.get("/management-portal/assets/portal.js")
    settings_script = client.get("/management-portal/assets/settings.js")
    stylesheet = client.get("/management-portal/assets/portal.css")
    assert (
        script.status_code
        == settings_script.status_code
        == stylesheet.status_code
        == 200
    )
    assert "data-role-assignment" in settings_script.text
    assert "createProfileEditor" in settings_script.text
    assert "syncInferenceInstanceForms" in settings_script.text
    assert "async function createInferenceInstance(event)" in settings_script.text
    assert "async function deleteInferenceInstance(instanceId)" in settings_script.text
    assert "method: 'POST'" in settings_script.text
    assert "method: 'DELETE'" in settings_script.text
    assert (
        "function renderModelSelection(form, selectedFilename)" in settings_script.text
    )
    assert "select.value = selectedFilename" in settings_script.text
    assert "(missing from shared model storage)" in settings_script.text
    assert "Choose an available model or request its download." in settings_script.text
    assert "Checking Hugging Face file metadata..." in settings_script.text
    assert "function suggestModelFilenameFromUrl()" in settings_script.text
    assert "parsed.pathname.split('/').pop()" in settings_script.text
    assert "decodeURIComponent(encodedBasename || '')" in settings_script.text
    assert "if (!modelDownloadFilenameManuallyEdited)" in settings_script.text
    assert "modelDownloadFilenameManuallyEdited = true" in settings_script.text
    request_handler = settings_script.text.split(
        "async function requestInferenceModelDownload(event)", 1
    )[1].split("async function pollInferenceDownloads()", 1)[0]
    assert request_handler.index(
        "showDownloadStatus('resolving')"
    ) < request_handler.index("await fetch('/api/inference-model-downloads'")
    assert "Hugging Face file could not be verified." in settings_script.text
    assert "Download queued; checksum metadata verified." in settings_script.text
    assert "downloadStatus.textContent = message;" in settings_script.text
    assert (
        "fetch(`/api/inference-model-downloads/${download.requestId}`"
        in settings_script.text
    )
    assert (
        "if (downloadBecameTerminal || rolloutActive) await loadInferenceInstances({ quiet: true });"
        in settings_script.text
    )
    assert "function showToast(message, state = 'info')" in settings_script.text
    assert "CPU: ${cpu} | GPU: ${gpu}" in settings_script.text
    assert "RAM: ${ram} | VRAM used: ${used}; total: ${total}" in settings_script.text
    assert ".inference-instance-list" in stylesheet.text
    assert (
        ".inference-instance-list {\n    display: flex;\n    flex-direction: column;"
        in stylesheet.text
    )
    assert (
        "display: flex;\n    flex-direction: column;\n    gap: 16px;" in stylesheet.text
    )
    assert (
        ".inference-model-config .config-controls input {\n    flex-basis: auto;"
        in stylesheet.text
    )
    assert ".config-controls input {\n    flex: 0 1 520px;" in stylesheet.text
    assert "#completion-timeout {\n    max-width: 180px;" in stylesheet.text
    assert "Save roster" in settings_page.text
    assert ":focus-visible {" in stylesheet.text
    assert "No workflow records are available yet." in script.text
    assert "Workflow state is unavailable." in script.text
    assert "fetch('/api/workflows'" in script.text
    assert "window.setInterval" in script.text
    assert "refreshIntervalMs = 5000" in script.text
    assert "visibilitychange" in script.text
    assert "cache: 'no-store'" in script.text
    assert "workflowFlowNodes" in script.text
    assert "workflowFlowLanes" in script.text
    assert "No CI result received yet" in script.text
    assert "Waiting for a human merge" in script.text
    assert "Completed" in script.text
    assert "renderExecutionFlow" in script.text
    assert "flow-node-status" in script.text
    assert "CI failure" in script.text
    assert "human-merge boundary" in script.text
    assert "flow-lane-nodes" in stylesheet.text
    assert "workflow.status === 'failed'" in script.text
    assert "/api/workflows/" in script.text
    assert "No CI checks found: operator decision required" in script.text
    assert "Proceed without CI" in script.text
    assert "resolve-no-ci" in script.text
    assert "Operator decision:" in script.text
    assert "#f3f6f5" in stylesheet.text


def test_inference_model_selection_builds_accessible_single_choice_dropdown(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        settings_page = client.get("/settings")
        settings_script = client.get("/management-portal/assets/settings.js")

    assert settings_page.status_code == settings_script.status_code == 200
    select_markup = settings_page.text.split('<select id="model-filename"', 1)[1].split(
        "</select>", 1
    )[0]
    assert 'name="model_filename"' in select_markup
    assert "required" in select_markup
    assert 'size="1"' in select_markup
    assert "multiple" not in select_markup
    assert '<button type="submit" disabled>Apply model</button>' in settings_page.text
    assert (
        'class="apply-model-help audit-line" role="status" aria-live="polite"'
        in settings_page.text
    )

    create_form = settings_script.text.split(
        "function createInferenceInstanceForm(instanceId)", 1
    )[1].split("function renderModelSelection", 1)[0]
    assert "selectionStatus.id = `${prefix}-model-selection-status`" in create_form
    assert (
        "form.elements.model_filename.setAttribute('aria-describedby', selectionStatus.id)"
        in create_form
    )
    assert "applyButton.textContent = 'Apply model'" in create_form
    assert "applyButton.setAttribute('aria-describedby', applyHelp.id)" in create_form

    save_state = settings_script.text.split(
        "function updateInferenceSaveState(form)", 1
    )[1].split("function updateModelDownloadButton()", 1)[0]
    assert "(unchanged && status === 'ready')" in save_state
    assert "status = form.dataset.rolloutStatus || 'not_requested'" in save_state
    assert "|| unchanged" not in save_state
    assert (
        "This filename is already selected. Choose a different available model to enable Apply model."
        in save_state
    )
    assert (
        "No rollout has been requested for this selection. Apply model to request one."
        in save_state
    )
    assert "The previous rollout failed. Apply model to retry." in save_state

    render_selection = settings_script.text.split(
        "function renderModelSelection(form, selectedFilename)", 1
    )[1].split("function syncInferenceInstanceForms", 1)[0]
    assert "document.createElement('option')" in render_selection
    assert "option.value = filename" in render_selection
    assert "option.textContent = filename" in render_selection
    assert "select.append(option)" in render_selection


def test_dashboard_and_api_require_a_valid_session(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        dashboard = client.get("/", follow_redirects=False)
        settings_page = client.get("/settings", follow_redirects=False)
        api = client.get("/api/workflows")

    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/auth/login"
    assert settings_page.status_code == 303
    assert settings_page.headers["location"] == "/auth/login"
    assert api.status_code == 401


def test_only_verified_organization_owner_can_update_agent_config(tmp_path):
    app = _create_app(tmp_path)
    requests = []

    def owner_oauth_response(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "role": "admin",
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "verified-owner"})
        return _oauth_response(request)

    def langgraph_response(request):
        requests.append(request)
        assert request.headers["Authorization"] == (
            "Bearer portal-internal-service-token"
        )
        if request.url.path.endswith("/agent-config") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "config": {
                        "completion_timeout_seconds": 45,
                    },
                    "version": 0,
                    "updated_at": None,
                    "updated_by": None,
                },
            )
        if request.url.path.endswith("/agent-config") and request.method == "PUT":
            payload = json.loads(request.content)
            assert payload == {
                "completion_timeout_seconds": 60,
                "updated_by": "verified-owner",
            }
            return httpx.Response(
                200,
                json={
                    "config": {
                        "completion_timeout_seconds": 60,
                    },
                    "version": 1,
                    "updated_at": "2026-11-02T12:00:00+00:00",
                    "updated_by": "verified-owner",
                },
            )
        if request.url.path.endswith("/executions"):
            return httpx.Response(
                200,
                json={
                    "executions": [
                        {
                            "thread_id": "task-safe",
                            "checkpoint_id": "checkpoint-safe",
                            "created_at": "2026-11-02T12:00:00Z",
                            "step": 2,
                            "workflow_status": "awaiting_ci",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/task-safe"):
            return httpx.Response(
                200,
                json={
                    "thread_id": "task-safe",
                    "checkpoints": [
                        {
                            "thread_id": "task-safe",
                            "checkpoint_id": "checkpoint-safe",
                            "created_at": "2026-11-02T12:00:00Z",
                            "step": 2,
                            "workflow_status": "awaiting_ci",
                            "next_nodes": ["await_ci"],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    app.state.oauth_http_transport = httpx.MockTransport(owner_oauth_response)
    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        config = client.get("/api/agent-config")
        secret_config = client.put(
            "/api/agent-config",
            headers={"Origin": "https://testserver"},
            json={
                "completion_timeout_seconds": 60,
                "api_key": "secret-marker",
            },
        )
        cross_origin_update = client.put(
            "/api/agent-config",
            headers={"Origin": "https://attacker.example"},
            json={
                "completion_timeout_seconds": 60,
            },
        )
        updated = client.put(
            "/api/agent-config",
            headers={"Origin": "https://testserver"},
            json={
                "completion_timeout_seconds": 60,
            },
        )
        executions = client.get("/api/executions")
        detail = client.get("/api/executions/task-safe")

    assert callback_response.status_code == 303
    assert config.status_code == 200 and config.json()["can_edit"] is True
    assert secret_config.status_code == 422
    assert cross_origin_update.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["updated_by"] == "verified-owner"
    assert executions.json()["executions"][0]["workflow_status"] == "awaiting_ci"
    assert detail.json()["checkpoints"][0]["step"] == 2
    assert detail.json()["checkpoints"][0]["next_nodes"] == ["await_ci"]
    assert len(requests) == 4
    assert "portal-internal-service-token" not in (
        config.text + updated.text + executions.text + detail.text
    )


def test_owner_can_view_and_update_agent_roster_through_portal(tmp_path):
    app = _create_app(tmp_path)
    requests = []
    current_roster = _roster_payload()
    updated_roster = _roster_payload()
    updated_roster["agents"][0]["description"] = "Updated context profile"

    def langgraph_response(request):
        requests.append(request)
        assert request.headers["Authorization"] == (
            "Bearer portal-internal-service-token"
        )
        if request.url.path.endswith("/agent-roster") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "roster": current_roster,
                    "revision": 4,
                    "updated_at": "2026-11-02T12:00:00+00:00",
                    "updated_by": "previous-owner",
                },
            )
        if request.url.path.endswith("/agent-roster") and request.method == "PUT":
            payload = json.loads(request.content)
            assert payload == {
                "roster": updated_roster,
                "expected_revision": 4,
                "updated_by": "verified-owner",
            }
            return httpx.Response(
                200,
                json={
                    "roster": updated_roster,
                    "revision": 5,
                    "updated_at": "2026-11-03T12:00:00+00:00",
                    "updated_by": "verified-owner",
                },
            )
        return httpx.Response(404)

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login_as_owner(client, app)
        page = client.get("/settings")
        current = client.get("/api/agent-roster")
        cross_origin = client.put(
            "/api/agent-roster",
            headers={"Origin": "https://attacker.example"},
            json={"roster": updated_roster, "expected_revision": 4},
        )
        updated = client.put(
            "/api/agent-roster",
            headers={"Origin": "https://testserver"},
            json={"roster": updated_roster, "expected_revision": 4},
        )

    assert callback_response.status_code == 303
    assert page.status_code == 200
    assert "Agent roster" in page.text
    assert "metadata only" in page.text
    assert current.status_code == 200
    assert current.json()["can_edit"] is True
    assert current.json()["revision"] == 4
    assert current.json()["roster"] == current_roster
    assert cross_origin.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["revision"] == 5
    assert updated.json()["updated_by"] == "verified-owner"
    assert [request.method for request in requests] == ["GET", "PUT"]
    assert "portal-internal-service-token" not in current.text + updated.text


def test_active_member_cannot_update_agent_roster(tmp_path):
    app = _create_app(tmp_path)
    forwarded = []
    roster = _roster_payload()

    def langgraph_response(request):
        forwarded.append(request)
        return httpx.Response(
            200,
            json={
                "roster": roster,
                "revision": 1,
                "updated_at": None,
                "updated_by": None,
            },
        )

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        current = client.get("/api/agent-roster")
        update = client.put(
            "/api/agent-roster",
            headers={"Origin": "https://testserver"},
            json={"roster": roster, "expected_revision": 1},
        )

    assert callback_response.status_code == 303
    assert current.status_code == 200
    assert current.json()["can_edit"] is False
    assert update.status_code == 403
    assert [request.method for request in forwarded] == ["GET"]


def test_agent_roster_maps_validation_conflict_and_service_errors(tmp_path):
    app = _create_app(tmp_path)
    requests = []

    def langgraph_response(request):
        requests.append(request)
        if request.method == "PUT" and len(requests) == 1:
            return httpx.Response(409, json={"detail": "private conflict detail"})
        if request.method == "PUT":
            return httpx.Response(422, json={"detail": "private validation detail"})
        return httpx.Response(503, json={"detail": "private service detail"})

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login_as_owner(client, app)
        roster = _roster_payload()
        invalid_roster = _roster_payload()
        invalid_roster["agents"][0]["tool_profile"] = "shell"
        invalid = client.put(
            "/api/agent-roster",
            headers={"Origin": "https://testserver"},
            json={"roster": invalid_roster, "expected_revision": 0},
        )
        conflict = client.put(
            "/api/agent-roster",
            headers={"Origin": "https://testserver"},
            json={"roster": roster, "expected_revision": 0},
        )
        rejected = client.put(
            "/api/agent-roster",
            headers={"Origin": "https://testserver"},
            json={"roster": roster, "expected_revision": 0},
        )
        unavailable = client.get("/api/agent-roster")

    assert invalid.status_code == 422
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "agent roster changed; reload before saving"
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == (
        "agent roster was rejected by the workflow service"
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "workflow service unavailable"
    assert "private" not in conflict.text + rejected.text + unavailable.text
    assert len(requests) == 3


def test_active_member_cannot_save_agent_config(tmp_path):
    app = _create_app(tmp_path)
    forwarded = []
    app.state.langgraph_http_transport = httpx.MockTransport(
        lambda request: (
            forwarded.append(request)
            or httpx.Response(
                200,
                json={
                    "config": {
                        "completion_timeout_seconds": 45,
                    },
                    "version": 0,
                    "updated_at": None,
                    "updated_by": None,
                },
            )
        )
    )

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        config = client.get("/api/agent-config")
        update = client.put(
            "/api/agent-config",
            headers={"Origin": "https://testserver"},
            json={
                "completion_timeout_seconds": 60,
            },
        )

    assert callback_response.status_code == 303
    assert config.status_code == 200
    assert config.json()["can_edit"] is False
    assert update.status_code == 403
    assert [request.method for request in forwarded] == ["GET"]


def test_unverifiable_github_owner_role_fails_closed(tmp_path):
    app = _create_app(tmp_path)

    def failed_role_lookup(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(failed_role_lookup)
    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)

    assert callback_response.status_code == 502
    assert not any(
        value.startswith("codefactory_dashboard_session=")
        for value in callback_response.headers.get_list("set-cookie")
    )


def test_local_preview_serves_live_data_without_auth_and_blocks_writes():
    settings = PortalSettings(
        internal_service_token="portal-preview-service-token",
        github_service_url="http://github-service",
        langgraph_service_url="http://langgraph-service",
    )
    app = create_preview_app(settings)
    github_requests = []
    langgraph_requests = []

    def github_response(request):
        github_requests.append(request)
        return httpx.Response(
            200,
            json={"workflows": [_workflow_summary(status="processing")]},
        )

    def langgraph_response(request):
        langgraph_requests.append(request)
        if request.url.path.endswith("/inference-models"):
            return httpx.Response(200, json={"models": ["live-model.gguf"]})
        if request.url.path.endswith("/agent-config"):
            return httpx.Response(
                200,
                json={
                    "config": {
                        "completion_timeout_seconds": 45,
                    },
                    "version": 2,
                    "updated_at": None,
                    "updated_by": None,
                },
            )
        if request.url.path.endswith("/agent-roster"):
            return httpx.Response(
                200,
                json={
                    "roster": _roster_payload(),
                    "revision": 3,
                    "updated_at": None,
                    "updated_by": "preview-owner",
                },
            )
        if request.url.path.endswith("/executions"):
            return httpx.Response(
                200,
                json={
                    "executions": [
                        {
                            "thread_id": "live-task",
                            "checkpoint_id": "checkpoint-2",
                            "created_at": "2026-11-02T12:05:00Z",
                            "step": 2,
                            "workflow_status": "running",
                            "waiting_for": None,
                            "task_status": "running",
                            "event_type": "projects_v2_item",
                            "event_status": "processing",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "thread_id": "live-task",
                "checkpoints": [
                    {
                        "thread_id": "live-task",
                        "checkpoint_id": "checkpoint-2",
                        "created_at": "2026-11-02T12:05:00Z",
                        "step": 2,
                        "workflow_status": "running",
                        "waiting_for": None,
                        "task_status": "running",
                        "event_type": "projects_v2_item",
                        "event_status": "processing",
                    }
                ],
            },
        )

    app.state.github_http_transport = httpx.MockTransport(github_response)
    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)

    with TestClient(app) as client:
        dashboard = client.get("/")
        settings_page = client.get("/settings")
        stylesheet = client.get("/management-portal/assets/portal.css")
        script = client.get("/management-portal/assets/portal.js")
        settings_script = client.get("/management-portal/assets/settings.js")
        workflows = client.get("/api/workflows")
        config = client.get("/api/agent-config")
        models = client.get("/api/inference-models")
        roster = client.get("/api/agent-roster")
        executions = client.get("/api/executions")
        detail = client.get("/api/executions/live-task")
        updated = client.put(
            "/api/agent-config",
            json={
                "completion_timeout_seconds": 60,
            },
        )
        updated_roster = client.put(
            "/api/agent-roster",
            json={"roster": _roster_payload(), "expected_revision": 3},
        )

    assert dashboard.status_code == 200
    assert "Codefactory workflow state" in dashboard.text
    assert settings_page.status_code == 200
    assert "agent-config-form" in settings_page.text
    assert stylesheet.status_code == script.status_code == 200
    assert settings_script.status_code == 200
    assert "/api/agent-config" in settings_script.text
    assert "Local preview; live backend data is read-only." in settings_script.text
    assert workflows.json()["workflows"][0]["task_id"] == "PVTI_safe-task-id"
    assert workflows.json()["workflows"][0]["status"] == "processing"
    assert config.json()["preview"] is True
    assert config.json()["can_edit"] is False
    assert config.json()["version"] == 2
    assert models.json() == {"models": ["live-model.gguf"], "preview": True}
    assert roster.status_code == 200
    assert roster.json()["revision"] == 3
    assert roster.json()["can_edit"] is False
    assert roster.json()["preview"] is True
    assert executions.json()["executions"][0]["workflow_status"] == "running"
    assert detail.json()["thread_id"] == "live-task"
    assert updated.status_code == 403
    assert updated_roster.status_code == 403
    assert [request.method for request in github_requests] == ["GET"]
    assert [request.method for request in langgraph_requests] == ["GET"] * 5
    assert all(
        request.headers["Authorization"] == "Bearer portal-preview-service-token"
        for request in github_requests + langgraph_requests
    )


def test_local_preview_revalidates_dashboard_and_review_link_bundle():
    app = create_preview_app(PortalSettings())

    with TestClient(app) as client:
        dashboard = client.get("/")
        script = client.get("/management-portal/assets/portal.js")

    assert dashboard.status_code == 200
    assert script.status_code == 200
    assert dashboard.headers["cache-control"] == "no-cache"
    assert script.headers["cache-control"] == "no-cache"
    assert (
        "appendReviewLink(executionStatus, workflowsByTaskId.get(execution.thread_id))"
        in script.text
    )
    assert "reviewLink.textContent = 'Review feedback'" in script.text
    assert "reviewLink.href = reviewUrl" in script.text


def test_local_preview_fails_closed_without_internal_service_token():
    app = create_preview_app(PortalSettings())
    with TestClient(app) as client:
        workflows = client.get("/api/workflows")
    assert workflows.status_code == 503


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "testclient"])
def test_local_preview_allows_loopback_clients(host):
    assert _is_loopback_client(host)


@pytest.mark.parametrize("host", [None, "192.0.2.10", "localhost"])
def test_local_preview_rejects_non_loopback_clients(host):
    assert not _is_loopback_client(host)


@pytest.mark.parametrize("role", [None, "sensitive-role-marker", 1, [], {}])
def test_invalid_organization_role_logs_fixed_reason_without_response_data(
    tmp_path, caplog, role
):
    app = _create_app(tmp_path)

    def failed_owner_lookup(request):
        if request.url.path == f"/user/memberships/orgs/{GITHUB_ORGANIZATION}":
            return httpx.Response(
                200,
                json={
                    "state": "active",
                    "role": role,
                    "organization": {"login": GITHUB_ORGANIZATION},
                },
            )
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(failed_owner_lookup)
    caplog.set_level(logging.WARNING, logger="management_portal.auth")

    with TestClient(app, base_url="https://testserver") as client:
        login_response, callback_response = _login(client)

    state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
    assert callback_response.status_code == 502
    assert "temporarily unavailable" in callback_response.text
    assert "phase=organization_membership_check" in caplog.text
    assert "exception_class=OrganizationRoleLookupError" in caplog.text
    assert "upstream_status=200" in caplog.text
    assert "reason_code=organization_role_shape_invalid" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    for sensitive_value in (
        "sensitive-role-marker",
        OAUTH_CLIENT_SECRET,
        OAUTH_ACCESS_TOKEN,
        "authorization-code",
        state,
        GITHUB_ORGANIZATION,
        GITHUB_OAUTH_CALLBACK_URL,
        "https://github.com",
    ):
        assert sensitive_value not in caplog.text
        assert sensitive_value not in callback_response.text


@pytest.mark.parametrize(
    ("failure_path", "phase"),
    [
        ("/login/oauth/access_token", "token_exchange"),
        (
            f"/user/memberships/orgs/{GITHUB_ORGANIZATION}",
            "organization_membership_check",
        ),
        ("/user", "user_lookup"),
    ],
)
def test_oauth_phase_failures_log_only_allowlisted_diagnostics(
    tmp_path, caplog, failure_path, phase
):
    app = _create_app(tmp_path)
    sensitive_error = "sensitive-upstream-error-marker"

    def failed_phase_response(request):
        if request.url.path == failure_path:
            return httpx.Response(502, text=sensitive_error)
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(failed_phase_response)
    caplog.set_level(logging.WARNING, logger="management_portal.auth")

    with TestClient(app, base_url="https://testserver") as client:
        login_response, callback_response = _login(client)

    state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
    assert callback_response.status_code == 502
    assert "temporarily unavailable" in callback_response.text
    assert phase in caplog.text
    assert "HTTPStatusError" in caplog.text
    assert "upstream_status=502" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    for sensitive_value in (
        OAUTH_CLIENT_SECRET,
        OAUTH_ACCESS_TOKEN,
        "authorization-code",
        state,
        sensitive_error,
        GITHUB_OAUTH_CALLBACK_URL,
    ):
        assert sensitive_value not in caplog.text
        assert sensitive_value not in callback_response.text


def test_dashboard_auth_fails_closed_without_oauth_configuration(tmp_path):
    app = _create_app(tmp_path, oauth=False)

    with TestClient(app) as client:
        dashboard = client.get("/")
        api = client.get("/api/workflows")
        login = client.get("/auth/login")

    assert dashboard.status_code == api.status_code == login.status_code == 503
    assert dashboard.json()["detail"] == "Dashboard authentication is not configured"


def test_oauth_state_is_required_and_callback_query_is_redacted(tmp_path):
    app = _create_app(tmp_path)
    callback_query_strings = []

    @app.middleware("http")
    async def capture_callback_query_string(request, call_next):
        response = await call_next(request)
        if request.url.path == "/auth":
            callback_query_strings.append(request.scope["query_string"])
        return response

    with TestClient(app, base_url="https://testserver") as client:
        login_response = client.get("/auth/login", follow_redirects=False)
        invalid_callback = client.get(
            "/auth",
            params={"code": "sensitive-auth-code", "state": "incorrect-state"},
        )

    assert invalid_callback.status_code == 400
    assert "sensitive-auth-code" not in invalid_callback.text
    assert "incorrect-state" not in invalid_callback.text
    assert callback_query_strings == [b""]
    state_cookie = next(
        value
        for value in login_response.headers.get_list("set-cookie")
        if value.startswith(f"{DASHBOARD_STATE_COOKIE}_")
    )
    assert f"Max-Age={DASHBOARD_STATE_SECONDS}" in state_cookie
    assert "secure" in state_cookie.lower()
    assert "httponly" in state_cookie.lower()
    assert "samesite=lax" in state_cookie.lower()


def test_oauth_state_supports_multiple_in_flight_login_attempts(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        first_login = client.get("/auth/login", follow_redirects=False)
        first_state = parse_qs(urlsplit(first_login.headers["location"]).query)[
            "state"
        ][0]
        second_login = client.get("/auth/login", follow_redirects=False)
        second_state = parse_qs(urlsplit(second_login.headers["location"]).query)[
            "state"
        ][0]
        first_callback = client.get(
            "/auth",
            params={"code": "authorization-code", "state": first_state},
            follow_redirects=False,
        )
        second_callback = client.get(
            "/auth",
            params={"code": "authorization-code", "state": second_state},
            follow_redirects=False,
        )

    assert first_callback.status_code == second_callback.status_code == 303


def test_oauth_session_cookie_is_secure_http_only_and_short_lived(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)

    cookie = _set_cookie(callback_response, "codefactory_dashboard_session")
    assert callback_response.status_code == 303
    assert f"Max-Age={DASHBOARD_SESSION_SECONDS}" in cookie
    assert "path=/" in cookie.lower()
    assert "secure" in cookie.lower()
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert OAUTH_ACCESS_TOKEN not in cookie


def test_logout_clears_the_dashboard_session_cookie(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
        logout_response = client.post("/auth/logout", follow_redirects=False)
        dashboard_response = client.get("/", follow_redirects=False)

    assert logout_response.status_code == 303
    cleared_cookie = _set_cookie(
        logout_response, "codefactory_dashboard_session"
    ).lower()
    assert "max-age=0" in cleared_cookie
    assert "secure" in cleared_cookie
    assert "httponly" in cleared_cookie
    assert dashboard_response.status_code == 303


def test_inactive_or_incorrect_organization_membership_is_denied(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="management_portal.auth")
    for status_code, membership in (
        (200, {"state": "pending", "organization": {"login": GITHUB_ORGANIZATION}}),
        (200, {"state": "active", "organization": {"login": "OtherOrganization"}}),
        (404, {}),
    ):
        app = _create_app(tmp_path)

        def response_for_membership(request):
            if request.url == "https://github.com/login/oauth/access_token":
                return httpx.Response(
                    200,
                    json={"access_token": OAUTH_ACCESS_TOKEN, "scope": "read:org"},
                )
            if request.url.path.endswith(
                f"/user/memberships/orgs/{GITHUB_ORGANIZATION}"
            ):
                return httpx.Response(status_code, json=membership)
            return httpx.Response(200, json={"login": "unapproved-member"})

        app.state.oauth_http_transport = httpx.MockTransport(response_for_membership)
        caplog.clear()
        with TestClient(app, base_url="https://testserver") as client:
            login_response, callback_response = _login(client)

        state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
        assert callback_response.status_code == 403
        assert OAUTH_ACCESS_TOKEN not in callback_response.text
        assert "phase=organization_membership_check" in caplog.text
        assert "exception_class=OrganizationMembershipDenied" in caplog.text
        assert f"upstream_status={status_code}" in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        for sensitive_value in (OAUTH_CLIENT_SECRET, OAUTH_ACCESS_TOKEN, state):
            assert sensitive_value not in caplog.text
        assert "authorization-code" not in caplog.text


def test_inference_instance_api_is_owner_only_and_forwards_scoped_updates(tmp_path):
    app = _create_app(tmp_path)
    forwarded = []

    def langgraph_response(request):
        forwarded.append(request)
        if request.url.path == "/internal/management/inference-models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        "qwen2.5-32b-instruct-q4_k_m.gguf",
                        "qwen2.5-0.5b-instruct-q4_k_m.gguf",
                        "second-gpu.gguf",
                        "new-gpu-model.gguf",
                    ]
                },
            )
        if request.method == "POST":
            assert request.url.path == "/internal/management/inference-instances"
            assert json.loads(request.content) == {
                "instance_id": "gpu-new",
                "model_filename": "new-gpu-model.gguf",
                "created_by": "verified-owner",
            }
            return httpx.Response(
                201,
                json={
                    "config": {
                        "instance_id": "gpu-new",
                        "model_filename": "new-gpu-model.gguf",
                        "rollout_status": "not_requested",
                    },
                    "version": 1,
                    "updated_at": "2026-11-04T12:00:00+00:00",
                    "updated_by": "verified-owner",
                },
            )
        if request.method == "DELETE":
            assert (
                request.url.path
                == "/internal/management/inference-instances/cpu-secondary"
            )
            return httpx.Response(
                200,
                json={"deleted": True, "instance_id": "cpu-secondary"},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "instances": [
                        {
                            "config": {
                                "instance_id": "gpu",
                                "model_filename": "qwen2.5-32b-instruct-q4_k_m.gguf",
                                "rollout_status": "pending",
                                "rollout_request_id": "00000000-0000-0000-0000-000000000001",
                                "rollout_error": None,
                            },
                            "version": 0,
                            "updated_at": None,
                            "updated_by": None,
                        },
                        {
                            "config": {
                                "instance_id": "cpu",
                                "model_filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
                                "rollout_status": "not_requested",
                                "rollout_request_id": None,
                                "rollout_error": None,
                            },
                            "version": 0,
                            "updated_at": None,
                            "updated_by": None,
                        },
                        {
                            "config": {
                                "instance_id": "gpu-secondary",
                                "model_filename": "second-gpu.gguf",
                                "rollout_status": "not_requested",
                            },
                            "version": 0,
                            "updated_at": None,
                            "updated_by": None,
                        },
                        {
                            "config": {
                                "instance_id": "cpu-secondary",
                                "model_filename": "second-cpu.gguf",
                                "rollout_status": "not_requested",
                            },
                            "version": 0,
                            "updated_at": None,
                            "updated_by": None,
                        },
                    ]
                },
            )
        body = json.loads(request.content)
        assert body == {
            "instance_id": "gpu-secondary",
            "model_filename": "new-gpu-model.gguf",
            "updated_by": "verified-owner",
        }
        return httpx.Response(
            200,
            json={
                "config": {
                    "instance_id": "gpu-secondary",
                    "model_filename": "new-gpu-model.gguf",
                    "rollout_status": "not_requested",
                },
                "version": 1,
                "updated_at": "2026-11-04T12:00:00+00:00",
                "updated_by": "verified-owner",
            },
        )

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        member_models = client.get("/api/inference-models")
        member_create = client.post(
            "/api/inference-instances",
            headers={"Origin": "https://testserver"},
            json={"instance_id": "gpu-new", "model_filename": "new-gpu-model.gguf"},
        )
        _login_as_owner(client, app)
        instances = client.get("/api/inference-instances")
        available_models = client.get("/api/inference-models")
        updated = client.put(
            "/api/inference-instances/gpu-secondary",
            headers={"Origin": "https://testserver"},
            json={"model_filename": "new-gpu-model.gguf"},
        )
        unavailable_update = client.put(
            "/api/inference-instances/gpu-secondary",
            headers={"Origin": "https://testserver"},
            json={"model_filename": "not-present.gguf"},
        )
        created = client.post(
            "/api/inference-instances",
            headers={"Origin": "https://testserver"},
            json={"instance_id": "gpu-new", "model_filename": "new-gpu-model.gguf"},
        )
        invalid_create = client.post(
            "/api/inference-instances",
            headers={"Origin": "https://testserver"},
            json={"instance_id": "gpu-new", "model_filename": "not-present.gguf"},
        )
        wrong_origin_delete = client.delete(
            "/api/inference-instances/cpu-secondary",
            headers={"Origin": "https://attacker.test"},
        )
        deleted = client.delete(
            "/api/inference-instances/cpu-secondary",
            headers={"Origin": "https://testserver"},
        )

    assert member_models.status_code == 403
    assert member_create.status_code == 403
    assert (
        instances.status_code
        == available_models.status_code
        == updated.status_code
        == 200
    )
    assert available_models.json()["models"][-1] == "new-gpu-model.gguf"
    assert "second-cpu.gguf" in [
        item["config"]["model_filename"] for item in instances.json()["instances"]
    ]
    assert "second-cpu.gguf" not in available_models.json()["models"]
    assert instances.json()["can_edit"] is True
    assert "download_status" not in instances.json()["instances"][0]["config"]
    assert instances.json()["instances"][0]["config"]["rollout_status"] == "pending"
    assert instances.json()["instances"][0]["config"]["rollout_request_id"] == (
        "00000000-0000-0000-0000-000000000001"
    )
    assert [
        item["config"]["model_filename"] for item in instances.json()["instances"]
    ] == [
        "qwen2.5-32b-instruct-q4_k_m.gguf",
        "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "second-gpu.gguf",
        "second-cpu.gguf",
    ]
    assert updated.json()["config"]["model_filename"] == "new-gpu-model.gguf"
    assert unavailable_update.status_code == 422
    assert created.status_code == 200
    assert created.json()["config"]["instance_id"] == "gpu-new"
    assert invalid_create.status_code == 422
    assert wrong_origin_delete.status_code == 403
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "instance_id": "cpu-secondary"}
    assert [request.method for request in forwarded] == [
        "GET",
        "GET",
        "GET",
        "PUT",
        "GET",
        "GET",
        "POST",
        "GET",
        "DELETE",
    ]
    assert [request.url.path for request in forwarded] == [
        "/internal/management/inference-instances",
        "/internal/management/inference-models",
        "/internal/management/inference-models",
        "/internal/management/inference-instances/gpu-secondary",
        "/internal/management/inference-models",
        "/internal/management/inference-models",
        "/internal/management/inference-instances",
        "/internal/management/inference-models",
        "/internal/management/inference-instances/cpu-secondary",
    ]


def test_model_download_and_metrics_routes_enforce_owner_and_project_status(tmp_path):
    app = _create_app(tmp_path)
    forwarded = []
    request_id = "00000000-0000-0000-0000-000000000001"

    def langgraph_response(request):
        forwarded.append(request)
        if request.method == "POST":
            assert request.url.path == "/internal/management/inference-model-downloads"
            payload = json.loads(request.content)
            if "token=" in payload["model_url"]:
                return httpx.Response(422, json={"detail": "request rejected"})
            assert payload == {
                "model_filename": "model.gguf",
                "model_url": "https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF"
                "?show_file_info=Ornith-1.5-35B-Q4_K_M.gguf",
                "requested_by": "verified-owner",
            }
            return httpx.Response(
                202,
                json={
                    "request_id": request_id,
                    "model_filename": "model.gguf",
                    "requested_by": "verified-owner",
                    "status": "queued",
                    "public_error": None,
                    "attempts": 0,
                    "created_at": "2026-11-04T12:00:00+00:00",
                    "updated_at": "2026-11-04T12:00:00+00:00",
                },
            )
        if request.url.path.endswith(request_id):
            return httpx.Response(
                200,
                json={
                    "request_id": request_id,
                    "model_filename": "model.gguf",
                    "requested_by": "verified-owner",
                    "status": "failed",
                    "public_error": "Model artifact SHA-256 did not match.",
                    "attempts": 1,
                    "created_at": "2026-11-04T12:00:00+00:00",
                    "updated_at": "2026-11-04T12:01:00+00:00",
                },
            )
        return httpx.Response(
            200,
            json={
                "instances": [
                    {
                        "instance_id": instance,
                        "cpu_utilization_percent": {
                            "value": None,
                            "status": "unavailable",
                        },
                        "ram_used_bytes": {"value": None, "status": "unavailable"},
                        "gpu_utilization_percent": {
                            "value": None,
                            "status": "unavailable",
                        },
                        "vram_used_bytes": {"value": None, "status": "unavailable"},
                        "vram_total_bytes": {"value": None, "status": "unavailable"},
                        "sampled_at": None,
                    }
                    for instance in ("gpu", "cpu", "gpu-secondary", "cpu-secondary")
                ]
            },
        )

    app.state.langgraph_http_transport = httpx.MockTransport(langgraph_response)
    with TestClient(app, base_url="https://testserver") as client:
        _login(client)
        member_read = client.get("/api/inference-instances")
        member_submit = client.post(
            "/api/inference-model-downloads",
            headers={"Origin": "https://testserver"},
            json={
                "model_filename": "model.gguf",
                "model_url": "https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF"
                "?show_file_info=Ornith-1.5-35B-Q4_K_M.gguf",
            },
        )
        member_status = client.get(f"/api/inference-model-downloads/{request_id}")
        _login_as_owner(client, app)
        submitted = client.post(
            "/api/inference-model-downloads",
            headers={"Origin": "https://testserver"},
            json={
                "model_filename": "model.gguf",
                "model_url": "https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF"
                "?show_file_info=Ornith-1.5-35B-Q4_K_M.gguf",
            },
        )
        malformed = client.post(
            "/api/inference-model-downloads",
            headers={"Origin": "https://testserver"},
            json={
                "model_filename": "model.gguf",
                "model_url": "https://huggingface.co/model?token=credential-marker",
            },
        )
        legacy_route = client.post(
            "/api/inference-instances/gpu/model-downloads",
            headers={"Origin": "https://testserver"},
            json={
                "model_filename": "model.gguf",
                "model_url": "https://huggingface.co/org/repo/model.gguf",
            },
        )
        status = client.get(f"/api/inference-model-downloads/{request_id}")
        metrics = client.get("/api/inference-metrics")

    assert (
        member_read.status_code
        == member_submit.status_code
        == member_status.status_code
        == 403
    )
    assert submitted.status_code == status.status_code == metrics.status_code == 200
    assert malformed.status_code == 422
    assert legacy_route.status_code == 404
    assert "credential-marker" not in malformed.text
    assert submitted.json()["status"] == "queued"
    assert "instance_id" not in submitted.json()
    assert status.json()["public_error"] == "Model artifact SHA-256 did not match."
    assert [item["instance_id"] for item in metrics.json()["instances"]] == [
        "gpu",
        "cpu",
        "gpu-secondary",
        "cpu-secondary",
    ]
    assert [request.method for request in forwarded] == [
        "POST",
        "POST",
        "GET",
        "GET",
    ]
