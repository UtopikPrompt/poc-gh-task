import json
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from githubkit._httpx import httpx
from github_service.events import (
    GitHubEventNormalizer,
    GitHubWebhook,
    ProjectEventConfig,
)
from github_service.github_app import GitHubAppClient


def _client(tmp_path, transport, project_owner_type="organization"):
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
        private_key_path=key_path,
        project_owner="example-org",
        project_owner_type=project_owner_type,
        project_number=7,
        transport=transport,
    )
    return client, public_key


def test_app_auth_project_status_update_and_latest_ci_results(tmp_path):
    requests = []
    public_key = None

    def handle(request):
        requests.append(request)
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
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

        assert request.headers["Authorization"] == "token installation-token"
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            if "query($owner:" in body["query"]:
                assert "organization(login: $owner)" in body["query"]
                assert body["variables"] == {"owner": "example-org", "number": 7}
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "target": {
                                "projectV2": {
                                    "id": "PVT_project",
                                    "fields": {
                                        "nodes": [
                                            {
                                                "id": "PVTSSF_status",
                                                "name": "Status",
                                                "options": [
                                                    {
                                                        "id": "option-ready",
                                                        "name": "Ready",
                                                    },
                                                    {
                                                        "id": "option-done",
                                                        "name": "Done",
                                                    },
                                                ],
                                            }
                                        ],
                                        "pageInfo": {"hasNextPage": False},
                                    },
                                }
                            }
                        }
                    },
                )
            if "query($itemId" in body["query"]:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "node": {
                                "id": "PVTI_item",
                                "isArchived": False,
                                "project": {"id": "PVT_project"},
                                "content": {
                                    "number": 42,
                                    "state": "OPEN",
                                    "title": "Example task",
                                    "body": "Details",
                                    "repository": {"nameWithOwner": "org/repo"},
                                },
                                "fieldValues": {
                                    "nodes": [
                                        {
                                            "name": "Ready",
                                            "optionId": "option-ready",
                                            "field": {
                                                "id": "PVTSSF_status",
                                                "name": "Status",
                                            },
                                        }
                                    ],
                                    "pageInfo": {"hasNextPage": False},
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
            page = request.url.params.get("page", "1")
            check_runs = {
                "1": [
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
                ],
                "2": [
                    {
                        "name": "docs",
                        "status": "completed",
                        "conclusion": "success",
                        "completed_at": "2025-01-04T00:00:00Z",
                        "app": {"id": 3},
                    }
                ],
            }.get(page, [])
            headers = {}
            if page in {"1", "2"}:
                next_page = str(int(page) + 1)
                headers["Link"] = (
                    f'<{request.url.copy_set_param("page", next_page)}>; rel="next"'
                )
            return httpx.Response(200, json={"check_runs": check_runs}, headers=headers)
        page = request.url.params.get("page", "1")
        statuses = {
            "1": [
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
            "2": [
                {
                    "context": "deploy",
                    "state": "success",
                    "updated_at": "2025-01-03T00:00:00Z",
                }
            ],
        }.get(page, [])
        headers = {}
        if page in {"1", "2"}:
            next_page = str(int(page) + 1)
            headers["Link"] = (
                f'<{request.url.copy_set_param("page", next_page)}>; rel="next"'
            )
        return httpx.Response(200, json=statuses, headers=headers)

    client, public_key = _client(tmp_path, httpx.MockTransport(handle))
    try:
        item = client.get_project_item("PVTI_item", 654)
        assert item["project_node_id"] == "PVT_project"
        assert item["status_option_id"] == "option-ready"
        assert item["status_option_name"] == "Ready"
        assert item["is_target_project"] is True
        assert item["is_archived"] is False
        assert item["done_option_name"] == "Done"
        assert item["issue"]["state"] == "OPEN"
        assert item["issue"]["repository"] == "org/repo"

        ci = client.get_pull_request_ci("org/repo", 17, 654)
        assert ci["pull_request_id"] == "9001"
        assert ci["ci_checks"] == [
            {"name": "tests", "status": "success"},
            {"name": "lint", "status": "pending"},
            {"name": "docs", "status": "success"},
            {"name": "legacy", "status": "success"},
            {"name": "deploy", "status": "success"},
        ]
        assert ci["ci_complete"] is False
        assert [
            request.url.params.get("page", "1")
            for request in requests
            if request.url.path.endswith(("/check-runs", "/statuses"))
        ] == ["1", "2", "3", "1", "2", "3"]

        task_state = {
            "project_node_id": "PVT_project",
            "project_item_id": "PVTI_item",
        }
        update = client.mark_task_done(task_state, 654)
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
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        assert request.headers["Authorization"] == "token installation-token"
        if request.url.path.endswith("/pulls/17"):
            return httpx.Response(
                200,
                json={"id": 9001, "head": {"sha": "abc123"}},
            )
        return httpx.Response(
            200,
            json={"check_runs": []},
            headers={"Link": '<https://attacker.example/collect?page=2>; rel="next"'},
        )

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        with pytest.raises(ValueError) as error:
            client.get_pull_request_ci("org/repo", 17, 654)
        assert "changed API host" in str(error.value)
        assert not any(
            request.url.path.endswith("/check-runs")
            and request.url.params.get("page") == "2"
            for request in requests
        )
    finally:
        client.close()


def test_completion_revalidates_merged_pr_identity_and_current_checks(tmp_path):
    client, _ = _client(tmp_path, httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        pull_request = {"id": 9001, "state": "closed", "merged": True}
        client._sdk_client = lambda *_args, **_kwargs: SimpleNamespace(
            rest=SimpleNamespace(
                pulls=SimpleNamespace(
                    get=lambda *_args: SimpleNamespace(json=lambda: pull_request)
                )
            )
        )
        client.get_pull_request_ci = lambda *_args: {
            "pull_request_id": "9001",
            "ci_checks": [{"name": "required", "status": "success"}],
            "ci_complete": True,
        }
        assert client.verify_pull_request_completion("org/repo", 17, "9001", 654) == {
            "verified": True
        }
        assert client.verify_pull_request_completion("org/repo", 17, "wrong", 654) == {
            "verified": False
        }

        client.get_pull_request_ci = lambda *_args: {
            "pull_request_id": "9001",
            "ci_checks": [{"name": "required", "status": "pending"}],
            "ci_complete": False,
        }
        assert client.verify_pull_request_completion("org/repo", 17, "9001", 654) == {
            "verified": False
        }
    finally:
        client.close()


def test_project_node_id_is_derived_from_resolved_webhook_item():
    class FakeAPI:
        def get_project_item(self, item_node_id, installation_id):
            assert item_node_id == "PVTI_item"
            assert installation_id == 654
            return {
                "project_node_id": "PVT_from_webhook",
                "status_field_name": "Status",
                "status_option_name": "Ready",
                "is_target_project": True,
                "issue": {"repository": "org/repo"},
            }

    class FakeStore:
        def task_context(self, _task_id):
            return None

    normalizer = GitHubEventNormalizer(FakeAPI(), ProjectEventConfig())
    event = normalizer(
        GitHubWebhook(
            event_name="projects_v2_item",
            delivery_id="delivery-1",
            installation_id=654,
            payload={
                "action": "edited",
                "projects_v2_item": {"node_id": "PVTI_item"},
            },
        ),
        store=FakeStore(),
    )

    assert event["project_node_id"] == "PVT_from_webhook"
    assert event["installation_id"] == 654


def test_project_event_cannot_switch_an_existing_tasks_installation():
    class FakeAPI:
        def get_project_item(self, *_args):
            raise AssertionError("mismatched installation must not access GitHub")

    class FakeStore:
        def task_context(self, _task_id):
            return {"installation_id": "654"}

    normalizer = GitHubEventNormalizer(FakeAPI(), ProjectEventConfig())
    event = normalizer(
        GitHubWebhook(
            event_name="projects_v2_item",
            delivery_id="delivery-2",
            installation_id=987,
            payload={
                "action": "edited",
                "projects_v2_item": {"node_id": "PVTI_item"},
            },
        ),
        store=FakeStore(),
    )

    assert event is None


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ([], "status field is missing or ambiguous"),
        (
            [
                {"id": "field-1", "name": "Status", "options": []},
                {"id": "field-2", "name": "Status", "options": []},
            ],
            "status field is missing or ambiguous",
        ),
        (
            [
                {
                    "id": "field-1",
                    "name": "Status",
                    "options": [{"id": "done", "name": "Done"}],
                }
            ],
            "option 'Ready' is missing or ambiguous",
        ),
        (
            [
                {
                    "id": "field-1",
                    "name": "Status",
                    "options": [{"id": "ready", "name": "Ready"}],
                }
            ],
            "option 'Done' is missing or ambiguous",
        ),
        (
            [
                {
                    "id": "field-1",
                    "name": "Status",
                    "options": [
                        {"id": "ready", "name": "Ready"},
                        {"id": "done-1", "name": "Done"},
                        {"id": "done-2", "name": "Done"},
                    ],
                }
            ],
            "option 'Done' is missing or ambiguous",
        ),
        (
            [
                {
                    "id": "field-1",
                    "name": "Status",
                    "options": [
                        {"id": "ready-1", "name": "Ready"},
                        {"id": "ready-2", "name": "Ready"},
                        {"id": "done", "name": "Done"},
                    ],
                }
            ],
            "option 'Ready' is missing or ambiguous",
        ),
    ],
)
def test_project_identity_rejects_missing_or_ambiguous_names(
    tmp_path,
    fields,
    message,
):
    def handle(request):
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        assert request.url.path == "/graphql"
        return httpx.Response(
            200,
            json={
                "data": {
                    "target": {
                        "projectV2": {
                            "id": "PVT_project",
                            "fields": {
                                "nodes": fields,
                                "pageInfo": {"hasNextPage": False},
                            },
                        }
                    }
                }
            },
        )

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        with pytest.raises(RuntimeError, match=message):
            client._project_identity(654)
    finally:
        client.close()


def test_project_identity_fails_closed_when_fields_are_truncated(tmp_path):
    def handle(_request):
        if _request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "target": {
                        "projectV2": {
                            "id": "PVT_project",
                            "fields": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": True},
                            },
                        }
                    }
                }
            },
        )

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        with pytest.raises(RuntimeError, match="fields are incomplete"):
            client._project_identity(654)
    finally:
        client.close()


def test_installation_tokens_are_cached_independently(tmp_path):
    token_requests = []
    token_counts = {}

    def handle(request):
        if request.url.path.endswith("/access_tokens"):
            installation_id = int(request.url.path.split("/")[3])
            token_counts[installation_id] = token_counts.get(installation_id, 0) + 1
            count = token_counts[installation_id]
            token_requests.append(installation_id)
            expires_at = (
                "2000-01-01T00:00:00Z"
                if installation_id == 654 and count == 1
                else "2099-01-01T00:00:00Z"
            )
            return httpx.Response(
                201,
                json={
                    "token": f"token-{installation_id}-{count}",
                    "expires_at": expires_at,
                },
            )
        return httpx.Response(200, json={"ok": True})

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        for installation_id in (654, 987, 654, 654):
            client.get_repository("example/project", installation_id)
        assert token_requests == [654, 987, 654]
    finally:
        client.close()


def test_request_timeout_is_applied_to_token_and_rest_requests(tmp_path):
    timeouts = []

    def handle(request):
        timeouts.append(request.extensions["timeout"])
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        return httpx.Response(200, json={"full_name": "example/project"})

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        client.get_repository("example/project", 654, timeout=1.25)
    finally:
        client.close()

    assert len(timeouts) == 2
    assert all(timeout["connect"] == 1.25 for timeout in timeouts)
    assert all(timeout["read"] == 1.25 for timeout in timeouts)


def test_http_errors_preserve_404_handling_and_disable_retries(tmp_path):
    attempts = {}

    def handle(request):
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        attempts[request.url.path] = attempts.get(request.url.path, 0) + 1
        if request.url.path.endswith("/git/ref/heads/missing"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500, json={"message": "Internal Server Error"})

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        assert client.get_ref("example/project", "missing", 654) is None
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.get_repository("example/project", 654)
        assert error.value.response.status_code == 500
    finally:
        client.close()

    assert attempts == {
        "/repos/example/project/git/ref/heads/missing": 1,
        "/repos/example/project": 1,
    }


def test_project_identity_uses_configured_user_owner_kind(tmp_path):
    def handle(request):
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        body = json.loads(request.content)
        assert "user(login: $owner)" in body["query"]
        assert body["variables"] == {"owner": "example-org", "number": 7}
        return httpx.Response(
            200,
            json={
                "data": {
                    "target": {
                        "projectV2": {
                            "id": "PVT_project",
                            "fields": {
                                "nodes": [
                                    {
                                        "id": "status-field",
                                        "name": "Status",
                                        "options": [
                                            {"id": "ready", "name": "Ready"},
                                            {"id": "done", "name": "Done"},
                                        ],
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False},
                            },
                        }
                    }
                }
            },
        )

    client, _ = _client(
        tmp_path,
        httpx.MockTransport(handle),
        project_owner_type="user",
    )
    try:
        assert client._project_identity(654) == (
            "PVT_project",
            "status-field",
            "ready",
            "done",
        )
    finally:
        client.close()


def test_git_data_and_pull_request_calls_use_non_force_installation_api(tmp_path):
    requests = []

    def pull_request():
        return {
            "id": 9001,
            "number": 17,
            "state": "open",
            "html_url": "https://github.example/example/project/pull/17",
            "repository": {"full_name": "example/project"},
            "head": {
                "ref": "codefactory-task-abc",
                "repo": {"full_name": "example/project"},
            },
            "base": {
                "ref": "main",
                "repo": {"full_name": "example/project"},
            },
        }

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        assert request.headers["Authorization"] == "token installation-token"
        if path == "/repos/example/project":
            return httpx.Response(
                200,
                json={"full_name": "example/project", "default_branch": "main"},
            )
        if path == "/repos/example/project/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base-commit"}})
        if path == "/repos/example/project/git/commits/base-commit":
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if path == "/repos/example/project/git/trees/base-tree":
            return httpx.Response(
                200,
                json={"truncated": False, "tree": []},
            )
        if path == "/repos/example/project/git/blobs":
            return httpx.Response(201, json={"sha": "blob-sha"})
        if path == "/repos/example/project/git/trees":
            return httpx.Response(201, json={"sha": "tree-sha"})
        if path == "/repos/example/project/git/commits":
            return httpx.Response(201, json={"sha": "task-commit"})
        if path == "/repos/example/project/git/refs":
            return httpx.Response(201, json={})
        if path == "/repos/example/project/git/refs/heads/codefactory-task-abc":
            return httpx.Response(200, json={})
        if path == "/repos/example/project/pulls":
            if request.method == "POST":
                return httpx.Response(201, json=pull_request())
            return httpx.Response(200, json=[pull_request()])
        if path == "/repos/example/project/pulls/17":
            return httpx.Response(200, json=pull_request())
        raise AssertionError(f"unexpected GitHub request: {request.method} {path}")

    client, _ = _client(tmp_path, httpx.MockTransport(handle))
    try:
        assert client.get_repository("example/project", 654)["default_branch"] == "main"
        assert (
            client.get_ref("example/project", "main", 654)["object"]["sha"]
            == "base-commit"
        )
        assert (
            client.get_commit("example/project", "base-commit", 654)["tree"]["sha"]
            == "base-tree"
        )
        client.get_tree("example/project", "base-tree", 654)
        assert client.create_blob("example/project", "text", 654)["sha"] == "blob-sha"
        assert (
            client.create_tree("example/project", "base-tree", [], 654)["sha"]
            == "tree-sha"
        )
        assert (
            client.create_commit(
                "example/project", "message", "tree-sha", "base-commit", 654
            )["sha"]
            == "task-commit"
        )
        client.create_ref("example/project", "codefactory-task-abc", "task-commit", 654)
        client.update_ref("example/project", "codefactory-task-abc", "task-commit", 654)
        assert (
            client.get_pull_request("example/project", 17, 654)["pull_request_id"]
            == "9001"
        )
        assert (
            client.find_pull_requests(
                "example/project", "codefactory-task-abc", "main", 654
            )[0]["head"]
            == "codefactory-task-abc"
        )
        assert (
            client.create_pull_request(
                repository="example/project",
                title="Task",
                body="Proposal",
                head="codefactory-task-abc",
                base="main",
                installation_id=654,
            )["pull_request_id"]
            == "9001"
        )
    finally:
        client.close()

    update = next(request for request in requests if request.method == "PATCH")
    assert json.loads(update.content) == {"sha": "task-commit", "force": False}
    blob = next(
        request for request in requests if request.url.path.endswith("/git/blobs")
    )
    assert json.loads(blob.content) == {"content": "dGV4dA==", "encoding": "base64"}
    commit = next(
        request for request in requests if request.url.path.endswith("/git/commits")
    )
    assert json.loads(commit.content)["parents"] == ["base-commit"]
