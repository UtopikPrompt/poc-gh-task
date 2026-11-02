import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from jsonschema import Draft202012Validator
from fastapi.testclient import TestClient
from github_service.bootstrap import create_app
from github_service.management_portal.auth import (
    DASHBOARD_SESSION_SECONDS,
    DASHBOARD_STATE_SECONDS,
    GITHUB_OAUTH_CALLBACK_URL,
    GITHUB_ORGANIZATION,
)
from github_service.settings import RuntimeSettings

OAUTH_CLIENT_SECRET = "oauth-client-secret-marker"
OAUTH_ACCESS_TOKEN = "oauth-access-token-marker"
SESSION_SIGNING_KEY = "dashboard-session-signing-key-test-value"
WORKFLOW_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "openapi"
    / "github-service.json"
)


def _create_app(tmp_path, *, oauth=True):
    settings = RuntimeSettings(
        github_webhook_secret="webhook-secret",
        database_path=tmp_path / "github.sqlite",
        github_oauth_client_id="oauth-client-id" if oauth else None,
        github_oauth_client_secret=OAUTH_CLIENT_SECRET if oauth else None,
        dashboard_session_signing_key=SESSION_SIGNING_KEY if oauth else None,
        internal_service_token="portal-internal-service-token",
    )
    app = create_app(normalize_event=lambda *_: None, settings=settings)
    app.state.oauth_http_transport = httpx.MockTransport(_oauth_response)
    app.state.langgraph_http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "config": {"completion_timeout_seconds": 45},
                "version": 0,
                "updated_at": None,
                "updated_by": None,
            },
        )
    )
    return app


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
                "organization": {"login": GITHUB_ORGANIZATION},
            },
        )
    if request.url.path == "/user":
        return httpx.Response(200, json={"login": "approved-member"})
    if request.url.path == "/graphql":
        return httpx.Response(
            200,
            json={
                "data": {
                    "organization": {
                        "membersWithRole": {
                            "nodes": [{"login": "approved-owner"}],
                            "pageInfo": {"endCursor": None, "hasNextPage": False},
                        }
                    }
                }
            },
        )
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
        "/auth/callback",
        params={"code": "authorization-code", "state": authorization_query["state"][0]},
        follow_redirects=False,
    )
    return login_response, callback_response


def _set_cookie(response, name):
    return next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{name}=")
    )


def test_workflow_dashboard_api_exposes_safe_summary_only(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
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

        response = client.get("/api/workflows")
        internal_response = client.get(
            "/internal/management/workflows",
            headers={"Authorization": "Bearer portal-internal-service-token"},
        )
        unauthorized_internal_response = client.get("/internal/management/workflows")

    assert response.status_code == 200
    assert internal_response.status_code == 200
    assert internal_response.json()["workflows"] == response.json()["workflows"]
    assert unauthorized_internal_response.status_code == 401
    workflows = response.json()["workflows"]
    contract = json.loads(WORKFLOW_SCHEMA_PATH.read_text())
    summary_schema = contract["components"]["schemas"]["WorkflowSummary"]
    Draft202012Validator(summary_schema).validate(workflows[0])
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
    assert OAUTH_CLIENT_SECRET not in response.text
    assert OAUTH_ACCESS_TOKEN not in response.text


def test_workflow_dashboard_reports_observed_ci_and_merge_status(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
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

        response = client.get("/api/workflows")

    assert response.status_code == 200
    workflow = response.json()["workflows"][0]
    contract = json.loads(WORKFLOW_SCHEMA_PATH.read_text())
    summary_schema = contract["components"]["schemas"]["WorkflowSummary"]
    Draft202012Validator(summary_schema).validate(workflow)
    assert workflow["latest_event"] == "merged"
    assert workflow["delivery_id"] == delivery_id
    assert workflow["delivery_status"] == "accepted"
    assert workflow["pull_request_merged"] is True
    assert workflow["ci_status"] == "success"
    assert workflow["ci_complete"] is True


def test_workflow_dashboard_page_is_served_by_github_service(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)
        assert callback_response.status_code == 303
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Codefactory workflow state" in response.text
    assert "/management-portal/assets/portal.css" in response.text
    assert "/management-portal/assets/portal.js" in response.text

    script = client.get("/management-portal/assets/portal.js")
    stylesheet = client.get("/management-portal/assets/portal.css")
    assert script.status_code == stylesheet.status_code == 200
    assert "No workflow records are available yet." in script.text
    assert "Workflow state is unavailable." in script.text
    assert "fetch('/api/workflows'" in script.text
    assert "#f3f6f5" in stylesheet.text


def test_dashboard_and_api_require_a_valid_session(tmp_path):
    app = _create_app(tmp_path)

    with TestClient(app, base_url="https://testserver") as client:
        dashboard = client.get("/", follow_redirects=False)
        api = client.get("/api/workflows")

    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/auth/login"
    assert api.status_code == 401


def test_only_verified_organization_owner_can_update_agent_config(tmp_path):
    app = _create_app(tmp_path)
    requests = []

    def owner_oauth_response(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "verified-owner"})
        if request.url.path == "/graphql":
            query = json.loads(request.content)
            assert "role: OWNER" in query["query"]
            if query["variables"]["cursor"] is None:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "organization": {
                                "membersWithRole": {
                                    "nodes": [{"login": "another-owner"}],
                                    "pageInfo": {
                                        "endCursor": "owner-page-2",
                                        "hasNextPage": True,
                                    },
                                }
                            }
                        }
                    },
                )
            assert query["variables"]["cursor"] == "owner-page-2"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "organization": {
                            "membersWithRole": {
                                "nodes": [{"login": "verified-owner"}],
                                "pageInfo": {
                                    "endCursor": None,
                                    "hasNextPage": False,
                                },
                            }
                        }
                    }
                },
            )
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
                    "config": {"completion_timeout_seconds": 45},
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
                    "config": {"completion_timeout_seconds": 60},
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
        cross_origin_update = client.put(
            "/api/agent-config",
            headers={"Origin": "https://attacker.example"},
            json={"completion_timeout_seconds": 60},
        )
        updated = client.put(
            "/api/agent-config",
            headers={"Origin": "https://testserver"},
            json={"completion_timeout_seconds": 60},
        )
        executions = client.get("/api/executions")
        detail = client.get("/api/executions/task-safe")

    assert callback_response.status_code == 303
    assert config.status_code == 200 and config.json()["can_edit"] is True
    assert cross_origin_update.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["updated_by"] == "verified-owner"
    assert executions.json()["executions"][0]["workflow_status"] == "awaiting_ci"
    assert detail.json()["checkpoints"][0]["step"] == 2
    assert len(requests) == 4
    assert "portal-internal-service-token" not in (
        config.text + updated.text + executions.text + detail.text
    )


def test_active_member_cannot_save_agent_config(tmp_path):
    app = _create_app(tmp_path)
    forwarded = []
    app.state.langgraph_http_transport = httpx.MockTransport(
        lambda request: (
            forwarded.append(request)
            or httpx.Response(
                200,
                json={
                    "config": {"completion_timeout_seconds": 45},
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
            json={"completion_timeout_seconds": 60},
        )

    assert callback_response.status_code == 303
    assert config.status_code == 200
    assert config.json()["can_edit"] is False
    assert update.status_code == 403
    assert [request.method for request in forwarded] == ["GET"]


def test_unverifiable_github_owner_role_fails_closed(tmp_path):
    app = _create_app(tmp_path)

    def failed_role_lookup(request):
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"errors": [{"message": "denied"}]})
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(failed_role_lookup)
    with TestClient(app, base_url="https://testserver") as client:
        _, callback_response = _login(client)

    assert callback_response.status_code == 502
    assert not any(
        value.startswith("codefactory_dashboard_session=")
        for value in callback_response.headers.get_list("set-cookie")
    )


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
        if request.url.path == "/auth/callback":
            callback_query_strings.append(request.scope["query_string"])
        return response

    with TestClient(app, base_url="https://testserver") as client:
        login_response = client.get("/auth/login", follow_redirects=False)
        invalid_callback = client.get(
            "/auth/callback",
            params={"code": "sensitive-auth-code", "state": "incorrect-state"},
        )

    assert invalid_callback.status_code == 400
    assert "sensitive-auth-code" not in invalid_callback.text
    assert "incorrect-state" not in invalid_callback.text
    assert callback_query_strings == [b""]
    state_cookie = _set_cookie(login_response, "codefactory_dashboard_oauth_state")
    assert f"Max-Age={DASHBOARD_STATE_SECONDS}" in state_cookie
    assert "secure" in state_cookie.lower()
    assert "httponly" in state_cookie.lower()
    assert "samesite=lax" in state_cookie.lower()


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


def test_inactive_or_incorrect_organization_membership_is_denied(tmp_path):
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
            if request.url.path == "/graphql":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "organization": {
                                "membersWithRole": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "endCursor": None,
                                        "hasNextPage": False,
                                    },
                                }
                            }
                        }
                    },
                )
            return httpx.Response(200, json={"login": "unapproved-member"})

        app.state.oauth_http_transport = httpx.MockTransport(response_for_membership)
        with TestClient(app, base_url="https://testserver") as client:
            _, callback_response = _login(client)

        assert callback_response.status_code == 403
        assert OAUTH_ACCESS_TOKEN not in callback_response.text


def test_webhook_remains_public_and_hmac_verified_without_dashboard_oauth(tmp_path):
    app = _create_app(tmp_path, oauth=False)
    body = b'{"installation":{"id":654}}'
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "projects_v2_item",
                "x-github-delivery": "dashboard-webhook-test",
                "x-hub-signature-256": f"sha256={signature}",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
