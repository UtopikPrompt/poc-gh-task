import hashlib
import json

import httpx
import pytest
from langgraph_service.integrations.github_client import (
    GitHubServiceClient,
    build_submit_proposal_operation,
)
from langgraph_service.workflow.task_graph import build_task_graph
from langgraph_service.workflow.workflow_operations import (
    _complete,
    _restore_hunk_from_base,
    make_operations,
)


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


def test_model_call_uses_selected_role_profile_from_task_snapshot():
    provider = ScriptedProvider([{"ok": True}])

    result = _complete(
        provider,
        {
            "agent_roster_snapshot": {
                "revision": 4,
                "profiles": {
                    "planning": {
                        "name": "Planner",
                        "description": "Builds implementation plans.",
                        "prompt_content": "Prefer a concise plan.",
                    }
                },
            }
        },
        prompt="Base instruction",
        response_schema={"type": "object"},
        max_tokens=20,
        role_id="planning",
    )

    assert result == {"ok": True}
    assert "Planner" in provider.prompts[0][0]
    assert "Builds implementation plans." in provider.prompts[0][0]
    assert "Prefer a concise plan." in provider.prompts[0][0]


def test_restore_hunk_matches_unique_proposal_context_after_line_shift():
    restored = _restore_hunk_from_base(
        "# Project\n├── source/\n└── scenarios/\n",
        "Added independently\n# Project\n−── source/\n└── scenarios/\n",
        ("@@ -1,3 +1,3 @@\n # Project\n-├── source/\n+−── source/\n └── scenarios/"),
    )

    assert restored == "Added independently\n# Project\n├── source/\n└── scenarios/\n"


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
                    "ci_checks": [{"name": "required-tests", "status": "pending"}],
                    "ci_complete": False,
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


def test_repair_uses_submitted_review_feedback_without_ci_failures():
    provider = ScriptedProvider([_proposal_change("update", "revised\n")])
    operations = make_operations(github_client=None, proposal_provider=provider)
    feedback = "Revert the unrelated README change."

    result = operations.repair_changes(
        {
            "context": {
                "files": [{"path": "src/task.py", "content": "original\n"}],
                "change_scope": {
                    "update_paths": ["src/task.py"],
                    "create_directories": ["src"],
                },
            },
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": [{"path": "src/task.py"}]},
            "review_feedback": feedback,
        }
    )

    assert result["changes"][0]["content"] == "revised\n"
    assert (
        '"review_feedback":"Revert the unrelated README change."'
        in (provider.prompts[0][0])
    )
    assert (
        "every reviewer feedback item as an acceptance criterion"
        in provider.prompts[0][0]
    )
    assert "non-no-op patch" in provider.prompts[0][0]


def test_review_repair_rejects_an_unchanged_proposal():
    change = {"operation": "update", "path": "src/task.py", "content": "same\n"}
    provider = ScriptedProvider(
        [
            {"changes": [change]},
            {"question": "Which behavior should the repair preserve?"},
        ]
    )
    operations = make_operations(github_client=None, proposal_provider=provider)

    result = operations.repair_changes(
        {
            "context": {
                "files": [{"path": "src/task.py", "content": "original\n"}],
                "change_scope": {
                    "update_paths": ["src/task.py"],
                    "create_directories": ["src"],
                },
            },
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": [change]},
            "review_feedback": "Address the requested correction.",
        }
    )

    assert result == {
        "outcome": "repair_unresolved",
        "question": "Which behavior should the repair preserve?",
    }


def test_review_repair_returns_model_question_when_feedback_is_ambiguous():
    provider = ScriptedProvider(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Should archived records follow the same retention rule?",
            }
        ]
    )
    operations = make_operations(github_client=None, proposal_provider=provider)

    result = operations.repair_changes(
        {
            "context": {"files": [], "change_scope": {"update_paths": []}},
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": []},
            "review_feedback": "Apply the new retention rule to all records.",
        }
    )

    assert result == {
        "outcome": "repair_unresolved",
        "question": "Should archived records follow the same retention rule?",
    }


def test_inline_review_question_selects_only_a_current_review_comment_id():
    provider = ScriptedProvider(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Should this README change be reverted?",
                "source_review_comment_id": "4461111142",
            }
        ]
    )
    operations = make_operations(github_client=None, proposal_provider=provider)

    result = operations.repair_changes(
        {
            "event_type": "review_changes_requested",
            "review_comment_ids": ["4461111142", "4461111143"],
            "context": {"files": [], "change_scope": {"update_paths": []}},
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": []},
            "review_feedback": "Inline comment [review comment ID: 4461111142] on README.md.",
        }
    )

    assert result == {
        "outcome": "repair_unresolved",
        "question": "Should this README change be reverted?",
        "source_review_comment_id": "4461111142",
    }
    assert provider.prompts[0][1]["oneOf"][1]["properties"]["source_review_comment_id"][
        "enum"
    ] == ["4461111142", "4461111143"]


def test_agent_repair_direction_is_passed_separately_from_review_feedback():
    provider = ScriptedProvider([_proposal_change("update", "reverted")])
    operations = make_operations(github_client=None, proposal_provider=provider)

    result = operations.repair_changes(
        {
            "event_type": "agent_repair_requested",
            "review_comment_ids": ["4461111142"],
            "repair_direction": "Revert the unrelated README change.",
            "inline_review_context": {
                "path": "README.md",
                "line": 34,
                "start_line": 28,
                "side": "RIGHT",
                "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
            },
            "context": {
                "files": [
                    {"path": "README.md", "content": "├── source/\n"},
                    {"path": "src/task.py", "content": "original"},
                ],
                "change_scope": {
                    "update_paths": ["README.md", "src/task.py"],
                    "create_directories": ["", "src"],
                },
            },
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": []},
        }
    )

    prompt = provider.prompts[0][0]
    prompt_data = json.loads(
        prompt.split("UNTRUSTED_TASK_DATA_JSON: ", 1)[1].split("\nReturn only", 1)[0]
    )
    assert result["changes"][0]["content"] == "reverted"
    assert '"repair_direction":"Revert the unrelated README change."' in prompt
    assert prompt_data["inline_review_context"] == {
        "path": "README.md",
        "line": 34,
        "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
        "start_line": 28,
        "side": "RIGHT",
    }
    assert "including the answer to any pending bot clarification" in prompt
    assert "do not ask which lines or what text to add" in prompt


def test_human_feedback_reply_reuses_inline_root_diff_and_pr_base_for_followup():
    requests = []
    source_comment_id = "4461111142"
    inline_context = {
        "path": "README.md",
        "line": 34,
        "start_line": 28,
        "side": "RIGHT",
        "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
    }

    def handle(request):
        requests.append(request)
        if request.url.path == "/internal/github/source-files":
            assert json.loads(request.content) == {
                "task_id": "task-1",
                "paths": ["README.md"],
                "pull_request_number": 17,
                "expected_pull_request_id": "9001",
            }
            return httpx.Response(
                200,
                json={"files": [{"path": "README.md", "content": "base README\n"}]},
            )
        if request.url.path == "/internal/github/request-human-feedback":
            body = json.loads(request.content)
            assert body["source_review_comment_id"] == int(source_comment_id)
            return httpx.Response(
                200,
                json={
                    "comment_id": f"bot-comment-{
                        sum(
                            item.url.path == '/internal/github/request-human-feedback'
                            for item in requests
                        )
                    }",
                    "created": True,
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
            {
                "outcome": "repair_unresolved",
                "question": "Should the README connector change be reverted?",
                "source_review_comment_id": source_comment_id,
            },
            {
                "outcome": "repair_unresolved",
                "question": "Should the unrelated footer also be restored?",
                "source_review_comment_id": source_comment_id,
            },
        ]
    )
    graph = build_task_graph(
        make_operations(github_client=client, proposal_provider=provider)
    )
    initial = {
        "task_id": "task-1",
        "event_id": "repair-event-1",
        "task_status": "ready",
        "event_type": "review_changes_requested",
        "workflow_status": "awaiting_ci",
        "waiting_for": "ci",
        "review_feedback": (
            "Inline comment [review comment ID: 4461111142] on README.md:34: "
            "Should the README connector change be reverted?"
        ),
        "pull_request_id": "9001",
        "pull_request": {"number": 17},
        "event_pull_request_id": "9001",
        "event_head_sha": "head-1",
        "review_comment_ids": [source_comment_id],
        "inline_review_contexts": {source_comment_id: inline_context},
        "context": {
            "files": [{"path": "README.md", "content": "stale default branch\n"}],
            "change_scope": {
                "update_paths": ["README.md"],
                "create_directories": [""],
            },
        },
        "plan": {"summary": "Implement the task."},
        "risk": {"summary": "Review the change."},
        "change": {"changes": [{"path": "README.md", "content": "changed\n"}]},
    }
    try:
        waiting = graph.invoke(initial)
        resumed = graph.invoke(
            {
                **waiting,
                "event_id": "reply-delivery-1",
                "event_type": "human_feedback_reply",
                "event_pull_request_id": "9001",
                "human_feedback_reply": (
                    "Restore README lines to PR base state, keep entries, "
                    "undo connector changes."
                ),
                "reply_to_comment_id": source_comment_id,
                "reply_comment_id": "4461111143",
                "reply_action": "created",
            }
        )
    finally:
        client.close()

    assert waiting["pending_question_source_comment_id"] == source_comment_id
    assert waiting["inline_review_context"] == inline_context
    assert resumed["pending_question_source_comment_id"] == source_comment_id
    assert (
        resumed["pending_question"] == "Should the unrelated footer also be restored?"
    )
    repair_prompt = provider.prompts[1][0]
    prompt_data = json.loads(
        repair_prompt.split("UNTRUSTED_TASK_DATA_JSON: ", 1)[1].split(
            "\nReturn only", 1
        )[0]
    )
    assert prompt_data["inline_review_context"] == inline_context
    assert prompt_data["context"]["files"] == [
        {"path": "README.md", "content": "base README\n"}
    ]
    assert prompt_data["human_feedback_reply"] == (
        "Restore README lines to PR base state, keep entries, undo connector changes."
    )
    assert "Restore README lines to PR base state" in prompt_data["repair_direction"]
    assert "do not ask which lines" in repair_prompt
    feedback_requests = [
        json.loads(request.content)
        for request in requests
        if request.url.path == "/internal/github/request-human-feedback"
    ]
    assert [item["source_review_comment_id"] for item in feedback_requests] == [
        int(source_comment_id),
        int(source_comment_id),
    ]
    assert all(
        request.url.path
        in {
            "/internal/github/source-files",
            "/internal/github/request-human-feedback",
        }
        for request in requests
    )


def test_clear_restore_proposal_uses_exact_pull_request_base_content():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/internal/github/source-files"
        assert json.loads(request.content) == {
            "task_id": "task-1",
            "paths": ["README.md"],
            "pull_request_number": 17,
            "expected_pull_request_id": "9001",
        }
        return httpx.Response(
            200,
            json={"files": [{"path": "README.md", "content": "base README\n"}]},
        )

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider(
        [
            {
                "changes": [
                    {
                        "operation": "update",
                        "path": "README.md",
                        "content": "base README\n",
                    }
                ]
            }
        ]
    )
    operations = make_operations(github_client=client, proposal_provider=provider)
    state = {
        "task_id": "task-1",
        "event_type": "human_feedback_reply",
        "pull_request_id": "9001",
        "pull_request": {"number": 17},
        "repair_source_comment_id": "4461111142",
        "review_comment_ids": ["4461111142"],
        "repair_direction": "Restore README lines to PR base state; keep entries.",
        "human_feedback_reply": "Undo connector changes.",
        "inline_review_context": {
            "path": "README.md",
            "line": 34,
            "start_line": 28,
            "side": "RIGHT",
            "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
        },
        "context": {
            "files": [{"path": "README.md", "content": "stale default branch\n"}],
            "change_scope": {
                "update_paths": ["README.md"],
                "create_directories": [""],
            },
        },
        "plan": {"summary": "Implement the task."},
        "risk": {"summary": "Review the change."},
        "change": {"changes": [{"path": "README.md", "content": "changed\n"}]},
    }
    try:
        result = operations.repair_changes(state)
    finally:
        client.close()

    assert result["changes"] == [
        {"operation": "update", "path": "README.md", "content": "base README\n"}
    ]
    prompt_data = json.loads(
        provider.prompts[0][0]
        .split("UNTRUSTED_TASK_DATA_JSON: ", 1)[1]
        .split("\nReturn only", 1)[0]
    )
    assert prompt_data["context"]["files"] == [
        {"path": "README.md", "content": "base README\n"}
    ]
    assert "Restore README lines to PR base state" in prompt_data["repair_direction"]
    assert len(requests) == 1


def test_agent_restore_to_pr_base_applies_verified_hunk_and_preserves_other_changes():
    requests = []
    base_content = "# Project\n├── source/\n└── scenarios/\n\nBase footer\n"

    def handle(request):
        requests.append(request)
        assert request.url.path == "/internal/github/source-files"
        assert request.headers["Authorization"] == "Bearer service-token"
        assert json.loads(request.content) == {
            "task_id": "task-1",
            "paths": ["README.md"],
            "pull_request_number": 17,
            "expected_pull_request_id": "9001",
        }
        return httpx.Response(
            200,
            json={"files": [{"path": "README.md", "content": base_content}]},
        )

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Which connector characters should be used?",
                "source_review_comment_id": "4462953227",
            }
        ]
    )
    operations = make_operations(github_client=client, proposal_provider=provider)
    state = {
        "task_id": "task-1",
        "event_type": "agent_repair_requested",
        "pull_request_id": "9001",
        "pull_request": {"number": 17},
        "review_comment_ids": ["4462953227"],
        "repair_direction": "Those changes wasn't requested. Revert.",
        "inline_review_context": {
            "path": "README.md",
            "line": 2,
            "side": "RIGHT",
            "diff_hunk": (
                "@@ -1,3 +1,3 @@\n"
                " # Project\n"
                "-├── source/\n"
                "+−── source/\n"
                " └── scenarios/"
            ),
        },
        "context": {
            "files": [{"path": "README.md", "content": "stale default branch\n"}],
            "change_scope": {
                "update_paths": ["README.md"],
                "create_directories": [""],
            },
        },
        "plan": {"summary": "Restore the selected README tree change."},
        "risk": {"summary": "Only the selected hunk is restored."},
        "change": {
            "changes": [
                {
                    "operation": "update",
                    "path": "README.md",
                    "content": (
                        "# Project\n"
                        "−── source/\n"
                        "└── scenarios/\n"
                        "\n"
                        "Unrelated proposed footer\n"
                    ),
                }
            ]
        },
    }
    try:
        result = operations.repair_changes(state)
    finally:
        client.close()

    assert result["changes"] == [
        {
            "operation": "update",
            "path": "README.md",
            "content": (
                "# Project\n├── source/\n└── scenarios/\n\nUnrelated proposed footer\n"
            ),
        }
    ]
    assert result["summary"] == "Restore the selected README tree change."
    assert provider.prompts == []
    assert len(provider.outputs) == 1
    assert [request.url.path for request in requests] == [
        "/internal/github/source-files"
    ]


def test_agent_revert_removes_added_tree_row_and_preserves_unrelated_changes():
    requests = []
    base_content = "# Project\n├── source/\n├── docs/\n└── tests/\n\nBase footer\n"

    def handle(request):
        requests.append(request)
        assert request.url.path == "/internal/github/source-files"
        return httpx.Response(
            200,
            json={"files": [{"path": "README.md", "content": base_content}]},
        )

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider([])
    operations = make_operations(github_client=client, proposal_provider=provider)
    state = {
        "task_id": "task-1",
        "event_type": "agent_repair_requested",
        "pull_request_id": "9001",
        "pull_request": {"number": 17},
        "review_comment_ids": ["4462953227"],
        "repair_direction": "Those changes wasn't requested. Revert.",
        "inline_review_context": {
            "path": "README.md",
            "line": 5,
            "side": "RIGHT",
            "diff_hunk": (
                "@@ -1,4 +1,5 @@\n"
                " # Project\n"
                " ├── source/\n"
                " ├── docs/\n"
                " └── tests/\n"
                "+└── scenarios/"
            ),
        },
        "context": {
            "files": [{"path": "README.md", "content": "stale default branch\n"}],
            "change_scope": {
                "update_paths": ["README.md"],
                "create_directories": [""],
            },
        },
        "plan": {"summary": "Restore the selected README tree change."},
        "risk": {"summary": "Only the selected hunk is restored."},
        "change": {
            "changes": [
                {
                    "operation": "update",
                    "path": "README.md",
                    "content": (
                        "# Project\n"
                        "├── source/\n"
                        "├── docs/\n"
                        "└── tests/\n"
                        "└── scenarios/\n"
                        "\n"
                        "Unrelated proposed footer\n"
                    ),
                }
            ]
        },
    }
    try:
        result = operations.repair_changes(state)
    finally:
        client.close()

    assert result["changes"] == [
        {
            "operation": "update",
            "path": "README.md",
            "content": (
                "# Project\n"
                "├── source/\n"
                "├── docs/\n"
                "└── tests/\n"
                "\n"
                "Unrelated proposed footer\n"
            ),
        }
    ]
    assert provider.prompts == []
    assert [request.url.path for request in requests] == [
        "/internal/github/source-files"
    ]


def test_agent_restore_to_pr_base_clarifies_when_hunk_does_not_match_proposal():
    requests = []
    base_content = "# Project\n├── source/\n└── scenarios/\n"

    def handle(request):
        requests.append(request)
        assert request.url.path == "/internal/github/source-files"
        return httpx.Response(
            200,
            json={"files": [{"path": "README.md", "content": base_content}]},
        )

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    provider = ScriptedProvider(
        [
            {
                "changes": [
                    {
                        "operation": "update",
                        "path": "README.md",
                        "content": "would have been model-generated\n",
                    }
                ]
            }
        ]
    )
    operations = make_operations(github_client=client, proposal_provider=provider)
    state = {
        "task_id": "task-1",
        "event_type": "agent_repair_requested",
        "pull_request_id": "9001",
        "pull_request": {"number": 17},
        "review_comment_ids": ["4462953227"],
        "repair_direction": "Restore README tree block to PR base; keep entries.",
        "inline_review_context": {
            "path": "README.md",
            "line": 2,
            "side": "RIGHT",
            "diff_hunk": (
                "@@ -1,3 +1,3 @@\n"
                " # Project\n"
                "-├── source/\n"
                "+−── source/\n"
                " └── scenarios/"
            ),
        },
        "context": {
            "files": [{"path": "README.md", "content": "stale default branch\n"}],
            "change_scope": {
                "update_paths": ["README.md"],
                "create_directories": [""],
            },
        },
        "plan": {"summary": "Restore the selected README tree change."},
        "risk": {"summary": "Only the selected hunk is restored."},
        "change": {
            "changes": [
                {
                    "operation": "update",
                    "path": "README.md",
                    "content": "# Project\nconnector differs\n└── scenarios/\n",
                }
            ]
        },
    }
    try:
        result = operations.repair_changes(state)
    finally:
        client.close()

    assert result == {
        "outcome": "repair_unresolved",
        "question": (
            "The selected diff hunk does not match both the current proposal and "
            "the verified PR base, so I left it unchanged. Should I retry against "
            "the latest pull request diff?"
        ),
        "source_review_comment_id": "4462953227",
    }
    assert provider.prompts == []
    assert len(provider.outputs) == 1
    assert [request.url.path for request in requests] == [
        "/internal/github/source-files"
    ]


def test_unambiguous_inline_revert_retries_noop_without_asking_for_text():
    changed = {
        "operation": "update",
        "path": "README.md",
        "content": "−── source/\n",
    }
    provider = ScriptedProvider(
        [
            {"changes": [changed]},
            {"changes": [{**changed, "content": "├── source/\n"}]},
        ]
    )
    operations = make_operations(github_client=None, proposal_provider=provider)

    result = operations.repair_changes(
        {
            "event_type": "agent_repair_requested",
            "review_comment_ids": ["4461111142"],
            "repair_direction": "Restore the selected line to its intended appearance.",
            "inline_review_context": {
                "path": "README.md",
                "line": 34,
                "start_line": 28,
                "side": "RIGHT",
                "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
            },
            "context": {
                "files": [{"path": "README.md", "content": "├── source/\n"}],
                "change_scope": {
                    "update_paths": ["README.md"],
                    "create_directories": [""],
                },
            },
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": [changed]},
        }
    )

    assert result["changes"][0]["content"] == "├── source/\n"
    assert len(provider.prompts) == 2
    retry_data = json.loads(
        provider.prompts[1][0]
        .split("UNTRUSTED_TASK_DATA_JSON: ", 1)[1]
        .split("\nReturn only", 1)[0]
    )
    assert retry_data["repair_context"]["inline_review_context"]["path"] == "README.md"
    assert "do not ask which lines or what text to add" in provider.prompts[1][0]


def test_agent_repair_preserves_inline_source_for_ambiguous_clarification():
    provider = ScriptedProvider(
        [
            {
                "outcome": "repair_unresolved",
                "question": "Which behavior should this correction preserve?",
                "source_review_comment_id": "4461111142",
            }
        ]
    )
    operations = make_operations(github_client=None, proposal_provider=provider)

    result = operations.repair_changes(
        {
            "event_type": "agent_repair_requested",
            "review_comment_ids": ["4461111142"],
            "repair_direction": "Revert the unrelated README change.",
            "inline_review_context": {
                "path": "README.md",
                "line": 34,
                "diff_hunk": "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/",
            },
            "context": {
                "files": [{"path": "README.md", "content": "├── source/\n"}],
                "change_scope": {
                    "update_paths": ["README.md"],
                    "create_directories": [""],
                },
            },
            "plan": {"summary": "Implement the requested task."},
            "risk": {"summary": "Review behavior and regressions."},
            "change": {"changes": []},
        }
    )

    assert result == {
        "outcome": "repair_unresolved",
        "question": "Which behavior should this correction preserve?",
        "source_review_comment_id": "4461111142",
    }
    assert provider.prompts[0][1]["oneOf"][1]["properties"]["source_review_comment_id"][
        "enum"
    ] == ["4461111142"]
    prompt_data = json.loads(
        provider.prompts[0][0]
        .split("UNTRUSTED_TASK_DATA_JSON: ", 1)[1]
        .split("\nReturn only", 1)[0]
    )
    assert prompt_data["inline_review_context"]["diff_hunk"] == (
        "@@ -28,7 +28,7 @@\n-├── source/\n+−── source/"
    )


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


def test_github_service_client_requests_pr_human_feedback():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/internal/github/request-human-feedback"
        assert request.headers["Authorization"] == "Bearer service-token"
        return httpx.Response(200, json={"comment_id": "comment-17", "created": True})

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    try:
        result = client.request_human_feedback(
            task_id="task-1",
            question="Should archived records be included?",
            dedupe_key="a" * 64,
            source_review_comment_id=4461111142,
        )
    finally:
        client.close()

    assert result == {"comment_id": "comment-17", "created": True}
    assert json.loads(requests[0].content) == {
        "task_id": "task-1",
        "question": "Should archived records be included?",
        "dedupe_key": "a" * 64,
        "source_review_comment_id": 4461111142,
    }


def test_agent_clarification_operation_forwards_source_and_event_dedupe_key():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"comment_id": "comment-17", "created": True})

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    operations = make_operations(
        github_client=client, proposal_provider=ScriptedProvider([])
    )
    try:
        result = operations.request_human_feedback(
            {
                "task_id": "task-1",
                "event_id": "agent-repair-event-1",
                "repair_question": "Which behavior should this correction preserve?",
                "source_review_comment_id": "4461111142",
            }
        )
    finally:
        client.close()

    assert result == {"comment_id": "comment-17", "created": True}
    assert json.loads(requests[0].content) == {
        "task_id": "task-1",
        "question": "Which behavior should this correction preserve?",
        "dedupe_key": hashlib.sha256(b"task-1:agent-repair-event-1").hexdigest(),
        "source_review_comment_id": 4461111142,
    }


def test_edited_review_reply_reuses_dedupe_key_for_same_clarification():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"comment_id": "comment-17", "created": False})

    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(handle),
    )
    operations = make_operations(
        github_client=client, proposal_provider=ScriptedProvider([])
    )
    base = {
        "task_id": "task-1",
        "event_type": "human_feedback_reply",
        "repair_question": "Which behavior should this correction preserve?",
        "source_review_comment_id": "4461111142",
    }
    try:
        operations.request_human_feedback({**base, "event_id": "reply-created"})
        operations.request_human_feedback({**base, "event_id": "reply-edited"})
    finally:
        client.close()

    assert requests[0]["dedupe_key"] == requests[1]["dedupe_key"]


def test_agent_clarification_without_source_fails_before_github_request():
    requests = []
    client = GitHubServiceClient(
        service_url="http://github-service",
        token="service-token",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(
                    200, json={"comment_id": "unexpected", "created": True}
                )
            )
        ),
    )
    operations = make_operations(
        github_client=client, proposal_provider=ScriptedProvider([])
    )
    try:
        with pytest.raises(ValueError, match="requires a source comment"):
            operations.request_human_feedback(
                {
                    "task_id": "task-1",
                    "event_id": "agent-repair-event-missing-source",
                    "event_type": "agent_repair_requested",
                    "repair_question": "Which behavior should this correction preserve?",
                }
            )
    finally:
        client.close()

    assert requests == []


def test_agent_timeout_override_reaches_existing_model_completion_call():
    class TimeoutProvider:
        def __init__(self):
            self.timeout_seconds = None

        def complete(
            self,
            *,
            prompt,
            response_schema,
            max_tokens,
            timeout_seconds=None,
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
            "agent_config": {
                "completion_timeout_seconds": 75,
            },
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
