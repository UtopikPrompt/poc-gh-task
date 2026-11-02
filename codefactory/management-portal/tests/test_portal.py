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
from management_portal.settings import GITHUB_OAUTH_CALLBACK_URL, PortalSettings

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
    settings = PortalSettings(
        github_oauth_client_id="oauth-client-id" if oauth else None,
        github_oauth_client_secret=OAUTH_CLIENT_SECRET if oauth else None,
        dashboard_session_signing_key=SESSION_SIGNING_KEY if oauth else None,
        internal_service_token="portal-internal-service-token",
    )
    app = create_app(settings=settings)
    app.state.oauth_http_transport = httpx.MockTransport(_oauth_response)
    app.state.github_http_transport = httpx.MockTransport(_github_response)
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
        "/auth",
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
    assert workflows == [private_summary]
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


def test_workflow_dashboard_page_is_served_by_management_portal(tmp_path):
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
        secret_config = client.put(
            "/api/agent-config",
            headers={"Origin": "https://testserver"},
            json={"completion_timeout_seconds": 60, "api_key": "secret-marker"},
        )
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
    assert secret_config.status_code == 422
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


def _owner_lookup_failure_cases():
    marker = "owner-response-sensitive-marker"
    connection = {
        "nodes": [],
        "pageInfo": {"endCursor": None, "hasNextPage": False},
    }

    def page(*, nodes=None, page_info=None):
        return {
            "data": {
                "organization": {
                    "membersWithRole": {
                        "nodes": connection["nodes"] if nodes is None else nodes,
                        "pageInfo": (
                            connection["pageInfo"] if page_info is None else page_info
                        ),
                    }
                }
            }
        }

    repeated_cursor_page = page(page_info={"endCursor": marker, "hasNextPage": True})
    page_limit_responses = [
        page(page_info={"endCursor": f"cursor-{index}", "hasNextPage": True})
        for index in range(50)
    ]
    return [
        pytest.param(
            [("text", marker)], "response_json_decode_failed", id="json-decode"
        ),
        pytest.param([("json", marker)], "response_not_object", id="non-object"),
        pytest.param(
            [("json", {"errors": [{"message": marker}]})],
            "graphql_errors_present",
            id="graphql-errors",
        ),
        pytest.param(
            [("json", {"errors": marker})],
            "graphql_errors_shape_invalid",
            id="graphql-errors-shape",
        ),
        pytest.param(
            [("json", {"data": marker})], "response_data_shape_invalid", id="data-shape"
        ),
        pytest.param(
            [("json", {"data": {"organization": marker}})],
            "organization_shape_invalid",
            id="organization-shape",
        ),
        pytest.param(
            [("json", {"data": {"organization": {"membersWithRole": marker}}})],
            "members_connection_shape_invalid",
            id="connection-shape",
        ),
        pytest.param(
            [("json", page(nodes=marker))], "nodes_shape_invalid", id="nodes-shape"
        ),
        pytest.param(
            [("json", page(page_info=marker))],
            "page_info_shape_invalid",
            id="page-info-shape",
        ),
        pytest.param(
            [("json", page(page_info={"endCursor": None, "hasNextPage": marker}))],
            "has_next_page_shape_invalid",
            id="has-next-page-shape",
        ),
        pytest.param(
            [("json", page(nodes=[marker]))], "node_shape_invalid", id="node-shape"
        ),
        pytest.param(
            [("json", page(nodes=[{"login": None}]))],
            "login_shape_invalid",
            id="login-shape",
        ),
        pytest.param(
            [("json", page(page_info={"hasNextPage": True}))],
            "cursor_missing",
            id="cursor-missing",
        ),
        pytest.param(
            [("json", page(page_info={"endCursor": 1, "hasNextPage": True}))],
            "cursor_shape_invalid",
            id="cursor-shape",
        ),
        pytest.param(
            [("json", repeated_cursor_page), ("json", repeated_cursor_page)],
            "cursor_repeated",
            id="cursor-repeated",
        ),
        pytest.param(
            [("json", response) for response in page_limit_responses],
            "page_limit_exceeded",
            id="page-limit",
        ),
    ]


@pytest.mark.parametrize(
    ("graphql_responses", "reason_code"), _owner_lookup_failure_cases()
)
def test_owner_role_lookup_failures_log_fixed_reasons_without_response_data(
    tmp_path, caplog, graphql_responses, reason_code
):
    app = _create_app(tmp_path)
    responses = iter(graphql_responses)

    def failed_owner_lookup(request):
        if request.url.path == "/graphql":
            kind, payload = next(responses)
            if kind == "text":
                return httpx.Response(200, text=payload)
            return httpx.Response(200, json=payload)
        return _oauth_response(request)

    app.state.oauth_http_transport = httpx.MockTransport(failed_owner_lookup)
    caplog.set_level(logging.WARNING, logger="management_portal.auth")

    with TestClient(app, base_url="https://testserver") as client:
        login_response, callback_response = _login(client)

    state = parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]
    assert callback_response.status_code == 502
    assert "temporarily unavailable" in callback_response.text
    assert "phase=owner_role_lookup" in caplog.text
    assert "exception_class=OwnerRoleLookupError" in caplog.text
    assert "upstream_status=200" in caplog.text
    assert f"reason_code={reason_code}" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    for sensitive_value in (
        "owner-response-sensitive-marker",
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
        ("/graphql", "owner_role_lookup"),
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
