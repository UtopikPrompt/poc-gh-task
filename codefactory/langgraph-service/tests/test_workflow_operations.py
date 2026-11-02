import json

import httpx
import pytest
from langgraph_service.github_client import (
    GitHubServiceClient,
    build_submit_proposal_operation,
)
from langgraph_service.task_graph import build_task_graph
from langgraph_service.workflow_operations import make_operations


class ScriptedProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, *, prompt, response_schema, max_tokens):
        self.prompts.append((prompt, response_schema, max_tokens))
        output = self.outputs.pop(0)
        return output


def _proposal_change(operation, content):
    return {
        "changes": [{"operation": operation, "path": "src/task.py", "content": content}]
    }


@pytest.mark.parametrize(
    ("operation", "path", "permitted", "message"),
    [
        ("update", "src/task.py", True, ""),
        ("create", "src/new.py", True, ""),
        ("update", "docs/unrelated.py", False, "outside the task change scope"),
        ("create", "docs/new.py", False, "outside the task change scope"),
        ("create", "src/.aws/credentials.py", False, "invalid path or operation"),
    ],
)
def test_changes_are_limited_to_retrieved_files_and_their_directories(
    operation, path, permitted, message
):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/internal/github/source-tree":
            return httpx.Response(
                200, json={"paths": [{"path": "src/task.py", "size": 14}]}
            )
        if request.url.path == "/internal/github/source-files":
            return httpx.Response(
                200,
                json={"files": [{"path": "src/task.py", "content": "print(1)\n"}]},
            )
        raise AssertionError(f"unexpected GitHub Service request: {request.url}")

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider(
        [
            {"paths": ["src/task.py"]},
            {"changes": [{"operation": operation, "path": path, "content": "updated"}]},
        ]
    )
    operations = make_operations(github_client=client, proposal_provider=provider)
    try:
        context = operations.gather_context(
            {"task_id": "task-1", "issue": {"title": "task", "body": ""}}
        )
        assert context["change_scope"] == {
            "update_paths": ["src/task.py"],
            "create_directories": ["src"],
        }
        state = {
            "context": context,
            "plan": {"summary": "Implement task"},
            "risk": {"summary": "Review the change"},
        }
        if permitted:
            assert operations.prepare_changes(state)["changes"][0]["path"] == path
        else:
            with pytest.raises(ValueError, match=message):
                operations.prepare_changes(state)
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/internal/github/source-tree",
        "/internal/github/source-files",
    ]


def test_graph_selects_task_paths_and_publishes_then_repairs_from_ci_failure():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer service-token"
        if request.url.path == "/internal/github/source-tree":
            return httpx.Response(
                200,
                json={
                    "paths": [
                        {"path": "README.md", "size": 7},
                        {"path": "src/task.py", "size": 14},
                    ]
                },
            )
        if request.url.path == "/internal/github/source-files":
            body = json.loads(request.content)
            assert body == {"task_id": "task-1", "paths": ["src/task.py"]}
            return httpx.Response(
                200,
                json={"files": [{"path": "src/task.py", "content": "print('old')\n"}]},
            )
        if request.url.path == "/internal/github/propose-changes":
            proposal = json.loads(request.content)
            assert proposal["task_id"] == "task-1"
            assert proposal["summary"] == "Change the task implementation."
            assert (
                proposal["risk_summary"]
                == "Focused code change; review behavior and regression impact."
            )
            assert proposal["changes"][0]["path"] == "src/task.py"
            return httpx.Response(
                200,
                json={
                    "pull_request_id": "pr-17",
                    "repository": "example/project",
                    "number": 17,
                },
            )
        raise AssertionError(f"unexpected GitHub Service request: {request.url}")

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider(
        [
            {"paths": ["src/task.py"]},
            {
                "summary": "Change the task implementation.",
                "steps": ["Update the task implementation."],
                "validation": ["Run repository checks."],
                "assumptions": [],
            },
            {
                "summary": "Focused code change; review behavior and regression impact.",
                "risks": ["Behavior may affect callers."],
            },
            _proposal_change("update", "print('first proposal')\n"),
            _proposal_change("update", "print('repaired proposal')\n"),
        ]
    )
    operations = make_operations(github_client=client, proposal_provider=provider)
    graph = build_task_graph(
        operations,
        publish_pr_operation=build_submit_proposal_operation(client),
    )
    try:
        ready = graph.invoke(
            {
                "task_id": "task-1",
                "task_status": "ready",
                "event_type": "ready",
                "target_repositories": ["example/project"],
                "issue": {
                    "number": 42,
                    "title": "Fix task behavior",
                    "body": "Please update the function.",
                    "repository": "example/project",
                },
                "installation_id": 654,
            }
        )
        repaired = graph.invoke(
            {
                **ready,
                "event_type": "ci",
                "event_pull_request_id": "pr-17",
                "ci_checks": [{"name": "required-tests", "status": "failure"}],
                "ci_complete": False,
            }
        )
    finally:
        client.close()

    assert ready["workflow_status"] == "awaiting_ci"
    assert ready["risk"]["summary"].startswith("Focused code change")
    assert repaired["workflow_status"] == "awaiting_ci"
    assert repaired["pull_request_id"] == "pr-17"
    assert [request.url.path for request in requests] == [
        "/internal/github/source-tree",
        "/internal/github/source-files",
        "/internal/github/propose-changes",
        "/internal/github/propose-changes",
    ]
    assert all(
        "installation-token" not in prompt
        and "service-token" not in prompt
        and "example/project" not in prompt
        for prompt, _, _ in provider.prompts
    )
    assert all(
        "UNTRUSTED_TASK_DATA_JSON" in prompt for prompt, _, _ in provider.prompts
    )
    assert (
        '"ci_failures":[{"name":"required-tests","status":"failure"}]'
        in provider.prompts[-1][0]
    )
    assert '"src/task.py"' in provider.prompts[-1][0]


def test_model_cannot_select_paths_outside_github_candidate_tree():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"paths": [{"path": "src/task.py", "size": 10}]}
        )

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider([{"paths": ["../../etc/passwd"]}])
    operations = make_operations(github_client=client, proposal_provider=provider)
    try:
        with pytest.raises(ValueError, match="outside the task candidate tree"):
            operations.gather_context(
                {
                    "task_id": "task-1",
                    "issue": {"title": "task", "body": ""},
                }
            )
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].url.path == "/internal/github/source-tree"


def test_agent_timeout_override_reaches_existing_model_completion_call():
    class TimeoutProvider:
        def __init__(self):
            self.timeout_seconds = None

        def complete(
            self, *, prompt, response_schema, max_tokens, timeout_seconds=None
        ):
            self.timeout_seconds = timeout_seconds
            return {
                "summary": "Implement the task.",
                "steps": ["Update the task."],
                "validation": ["Run tests."],
                "assumptions": [],
            }

    provider = TimeoutProvider()
    operations = make_operations(github_client=object(), proposal_provider=provider)

    result = operations.create_plan(
        {
            "agent_config": {"completion_timeout_seconds": 75},
            "context": {"files": []},
        }
    )

    assert result["summary"] == "Implement the task."
    assert provider.timeout_seconds == 75


def test_secret_like_issue_content_fails_before_github_or_model_requests():
    requests = []
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request) or httpx.Response(200, json={"paths": []})
            )
        ),
    )
    provider = ScriptedProvider([])
    operations = make_operations(github_client=client, proposal_provider=provider)
    try:
        with pytest.raises(ValueError, match="secret material"):
            operations.gather_context(
                {
                    "task_id": "task-1",
                    "issue": {
                        "title": "Review config",
                        "body": '{"password": "secret phrase with spaces"}',
                    },
                }
            )
    finally:
        client.close()

    assert requests == []
    assert provider.prompts == []
