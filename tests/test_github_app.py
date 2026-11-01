import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from codefactory.github_service.github_app import GitHubAppClient
from codefactory.langgraph_service.task_graph import TaskState


def _client(tmp_path, transport):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_path = tmp_path / "app-key.pem"
    key_path.write_bytes(pem)
    client = GitHubAppClient(
        app_id=321,
        installation_id=654,
        private_key_path=key_path,
        status_field_id="PVTSSF_status",
        done_option_id="option-done",
        transport=transport,
    )
    return client, public_key


def test_app_auth_project_status_update_and_latest_ci_results(tmp_path):
    requests = []
    public_key = None

    def handle(request):
        requests.append(request)
        if request.url.path == "/app/installations/654/access_tokens":
            app_jwt = request.headers["Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(app_jwt, public_key, algorithms=["RS256"])
            assert claims["iss"] == "321"
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )

        assert request.headers["Authorization"] == "Bearer installation-token"
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            if "query($itemId" in body["query"]:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "node": {
                                "id": "PVTI_item",
                                "project": {"id": "PVT_project"},
                                "content": {
                                    "number": 42,
                                    "title": "Example task",
                                    "body": "Details",
                                    "repository": {"nameWithOwner": "org/repo"},
                                },
                                "fieldValues": {
                                    "nodes": [
                                        {
                                            "name": "Ready",
                                            "optionId": "option-ready",
                                            "field": {"id": "PVTSSF_status"},
                                        }
                                    ]
                                },
                            }
                        }
                    },
                )
            assert body["variables"]["optionId"] == "option-done"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "updateProjectV2ItemFieldValue": {
                            "projectV2Item": {"id": "PVTI_item"}
                        }
                    }
                },
            )

        if request.url.path.endswith("/pulls/17"):
            return httpx.Response(
                200,
                json={"id": 9001, "head": {"sha": "abc123"}},
            )
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "failure",
                            "completed_at": "2025-01-01T00:00:00Z",
                            "app": {"id": 1},
                        },
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "success",
                            "completed_at": "2025-01-02T00:00:00Z",
                            "app": {"id": 1},
                        },
                        {
                            "name": "lint",
                            "status": "in_progress",
                            "conclusion": None,
                            "completed_at": None,
                            "started_at": "2025-01-03T00:00:00Z",
                            "created_at": "2025-01-03T00:00:00Z",
                            "app": {"id": 2},
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "context": "legacy",
                    "state": "failure",
                    "updated_at": "2025-01-01T00:00:00Z",
                },
                {
                    "context": "legacy",
                    "state": "success",
                    "updated_at": "2025-01-02T00:00:00Z",
                },
            ],
        )

    client, public_key = _client(tmp_path, httpx.MockTransport(handle))
    try:
        item = client.get_project_item("PVTI_item")
        assert item["project_node_id"] == "PVT_project"
        assert item["status_option_id"] == "option-ready"
        assert item["issue"]["repository"] == "org/repo"

        ci = client.get_pull_request_ci("org/repo", 17)
        assert ci["pull_request_id"] == "9001"
        assert ci["ci_checks"] == [
            {"name": "tests", "status": "success"},
            {"name": "lint", "status": "pending"},
            {"name": "legacy", "status": "success"},
        ]
        assert ci["ci_complete"] is False

        task_state: TaskState = {
            "project_node_id": "PVT_project",
            "project_item_id": "PVTI_item",
        }
        update = client.mark_task_done(task_state)
        assert "updateProjectV2ItemFieldValue" in update
        assert (
            len(
                [
                    request
                    for request in requests
                    if "/access_tokens" in request.url.path
                ]
            )
            == 1
        )
    finally:
        client.close()


def test_pagination_cannot_redirect_installation_token_to_another_host(tmp_path):
    def handle(request):
        return httpx.Response(
            200,
            json={"statuses": []},
            headers={"Link": '<https://attacker.example/collect?page=2>; rel="next"'},
        )

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    client._installation_token = lambda: "installation-token"
    try:
        try:
            client._list_pages("/statuses", "statuses", {})
        except ValueError as error:
            assert "changed API host" in str(error)
        else:
            raise AssertionError("external pagination link should be rejected")
    finally:
        client.close()


def test_completion_revalidates_merged_pr_identity_and_current_checks(tmp_path):
    client, _ = _client(tmp_path, httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        client._request = lambda *_args, **_kwargs: {
            "id": 9001,
            "state": "closed",
            "merged": True,
        }
        client.get_pull_request_ci = lambda *_args: {
            "pull_request_id": "9001",
            "ci_checks": [{"name": "required", "status": "success"}],
            "ci_complete": True,
        }
        assert client.verify_pull_request_completion("org/repo", 17, "9001") == {
            "verified": True
        }
        assert client.verify_pull_request_completion("org/repo", 17, "wrong") == {
            "verified": False
        }

        client.get_pull_request_ci = lambda *_args: {
            "pull_request_id": "9001",
            "ci_checks": [{"name": "required", "status": "pending"}],
            "ci_complete": False,
        }
        assert client.verify_pull_request_completion("org/repo", 17, "9001") == {
            "verified": False
        }
    finally:
        client.close()
