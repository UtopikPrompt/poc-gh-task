import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from github_service.bootstrap import create_app, create_github_app
from github_service.completion import GitHubTaskCompletion
from github_service.events import (
    GitHubEventNormalizer,
    GitHubWebhook,
    ProjectEventConfig,
)
from github_service.publishing import FileChangeProposal, GitHubTaskPublisher
from github_service.settings import RuntimeSettings
from langgraph_service.operations import WorkflowOperations
from langgraph_service.service import LangGraphTaskService
from langgraph_service.worker import LangGraphEventWorker


class FakeGitHubEventAPI:
    def __init__(self, *, ci_status="success", ci_complete=True):
        self.ci_status = ci_status
        self.ci_complete = ci_complete
        self.completed_items = []
        self.refs = {"main": "base-commit"}
        self.commits = {"base-commit": {"tree": "base-tree", "parents": []}}
        self.trees = {"base-tree": {}}
        self.blobs = {}
        self.pull_requests = []

    def get_project_item(self, item_node_id, installation_id):
        assert item_node_id == "PVTI_test_item"
        assert installation_id == 654
        return {
            "project_node_id": "PVT_test_project",
            "project_owner": "example-org",
            "project_owner_type": "organization",
            "status_field_name": "Status",
            "status_option_name": "Ready",
            "done_option_name": "Done",
            "is_archived": False,
            "issue": {
                "number": 42,
                "state": "OPEN",
                "title": "Example task",
                "body": "Implement the example task.",
                "repository": "example/project",
            },
        }

    def get_pull_request_ci(self, repository, pull_request_number, installation_id):
        assert repository == "example/project"
        assert pull_request_number == 17
        assert installation_id == 654
        return {
            "pull_request_id": "9001",
            "ci_checks": [{"name": "required-tests", "status": self.ci_status}],
            "ci_complete": self.ci_complete,
        }

    def get_repository(self, repository, installation_id):
        assert repository == "example/project"
        assert installation_id == 654
        return {"full_name": repository, "default_branch": "main"}

    def get_ref(self, repository, branch, installation_id):
        sha = self.refs.get(branch)
        return None if sha is None else {"object": {"sha": sha}}

    def get_commit(self, repository, sha, installation_id):
        return {"tree": {"sha": self.commits[sha]["tree"]}}

    def get_tree(self, repository, sha, installation_id):
        return {"truncated": False, "tree": list(self.trees[sha].values())}

    def create_blob(self, repository, content, installation_id):
        encoded = content.encode()
        blob_sha = hashlib.sha1(
            b"blob " + str(len(encoded)).encode() + b"\0" + encoded
        ).hexdigest()
        self.blobs[blob_sha] = content
        return {"sha": blob_sha}

    def create_tree(self, repository, base_tree, entries, installation_id):
        tree_sha = f"tree-{len(self.trees)}"
        tree = dict(self.trees[base_tree])
        tree.update({entry["path"]: dict(entry) for entry in entries})
        self.trees[tree_sha] = tree
        return {"sha": tree_sha}

    def create_commit(self, repository, message, tree, parent, installation_id):
        commit_sha = f"commit-{len(self.commits)}"
        self.commits[commit_sha] = {"tree": tree, "parents": [parent]}
        return {"sha": commit_sha}

    def create_ref(self, repository, branch, sha, installation_id):
        self.refs[branch] = sha

    def update_ref(self, repository, branch, sha, installation_id):
        assert self.commits[sha]["parents"] == [self.refs[branch]]
        self.refs[branch] = sha

    def get_pull_request(self, repository, number, installation_id):
        return next(pr for pr in self.pull_requests if pr["number"] == number)

    def find_pull_requests(self, repository, branch, base, installation_id):
        return [
            pr
            for pr in self.pull_requests
            if pr["head"] == branch and pr["base"] == base
        ]

    def create_pull_request(self, **request):
        assert request["repository"] == "example/project"
        assert request["installation_id"] == 654
        result = {
            "pull_request_id": "9001",
            "repository": request["repository"],
            "head_repository": request["repository"],
            "number": 17,
            "head": request["head"],
            "base": request["base"],
            "state": "open",
        }
        self.pull_requests.append(result)
        return result

    def verify_pull_request_completion(
        self, repository, number, expected_id, installation_id
    ):
        ci = self.get_pull_request_ci(repository, number, installation_id)
        return {
            "verified": expected_id == ci["pull_request_id"]
            and self.ci_complete
            and self.ci_status == "success"
        }

    def mark_task_done(self, state, installation_id):
        assert installation_id == 654
        self.completed_items.append(state["project_item_id"])
        return {"updated": True}


class LocalGitHubQueue:
    def __init__(self, store, github):
        self._store = store
        self._completion = GitHubTaskCompletion(
            store=store,
            github=github,
        )

    def claim_event(self):
        return self._store.claim_event()

    def acknowledge_event(self, event_id, claim_token):
        assert self._store.acknowledge_event(event_id, claim_token)

    def retry_event(self, event_id, claim_token, error):
        assert self._store.retry_event(event_id, claim_token, error)

    def complete_task(self, task_id):
        return self._completion(task_id)


def _local_task_service(tmp_path, store, github):
    operations = WorkflowOperations(
        gather_context=lambda _: {"summary": "Context gathered"},
        create_plan=lambda _: {"summary": "Plan created"},
        assess_risk=lambda _: {"summary": "Low risk"},
        prepare_changes=lambda _: {
            "changes": [
                {
                    "operation": "create",
                    "path": "src/example.py",
                    "content": "example = True\n",
                }
            ]
        },
    )
    publisher = GitHubTaskPublisher(store=store, github=github)

    def publish_pr(state):
        change = state["change"]
        return publisher.publish(
            FileChangeProposal.from_payload(
                {"task_id": state["task_id"], "changes": change["changes"]}
            )
        )

    return LangGraphTaskService(
        tmp_path / "langgraph.sqlite",
        operations,
        publish_pr=publish_pr,
    )


def _signature(body, secret="integration-secret"):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _deliver(client, event_name, delivery_id, payload, *, secret="integration-secret"):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "x-github-event": event_name,
            "x-github-delivery": delivery_id,
            "x-hub-signature-256": _signature(body, secret),
        },
    )


def test_event_normalizer_only_requires_pull_request_lookup():
    class PullRequestLookup:
        def task_for_pull_request(self, repository, pull_request_number):
            assert repository == "example/project"
            assert pull_request_number == 17
            return "PVTI_test_item"

        def task_context(self, _task_id):
            return {"installation_id": "654"}

    normalizer = GitHubEventNormalizer(
        FakeGitHubEventAPI(),
        ProjectEventConfig(),
    )
    webhook = GitHubWebhook(
        event_name="pull_request",
        delivery_id="delivery-merged",
        installation_id=654,
        payload={
            "action": "closed",
            "number": 17,
            "repository": {"full_name": "example/project"},
            "pull_request": {"id": 9001, "merged": True},
        },
    )

    assert normalizer(webhook, PullRequestLookup()) == {
        "task_id": "PVTI_test_item",
        "event_type": "merged",
        "installation_id": 654,
        "event_pull_request_id": "9001",
        "merged": True,
    }


def test_signed_github_deliveries_run_ready_ci_merge_lifecycle(tmp_path):
    github = FakeGitHubEventAPI()
    normalizer = GitHubEventNormalizer(
        github,
        ProjectEventConfig(),
    )
    app = create_app(
        normalize_event=normalizer,
        settings=RuntimeSettings(
            github_webhook_secret="integration-secret",
            database_path=tmp_path / "workflow.sqlite",
        ),
    )

    with TestClient(app) as client:
        store = client.app.state.internal_runtime.store
        service = _local_task_service(tmp_path, store, github)
        with service as lifecycle:
            worker = LangGraphEventWorker(
                queue=LocalGitHubQueue(store, github),
                lifecycle=lifecycle,
            )
            ready = _deliver(
                client,
                "projects_v2_item",
                "delivery-ready",
                {
                    "action": "edited",
                    "installation": {"id": 654},
                    "projects_v2_item": {
                        "id": 71,
                        "node_id": "PVTI_test_item",
                        "project_node_id": "PVT_test_project",
                    },
                    "changes": {"field_value": {"to": "Ready"}},
                },
            )
            assert ready.status_code == 200
            assert ready.json()["status"] == "queued"
            assert worker.process_one()

            ci = _deliver(
                client,
                "check_run",
                "delivery-ci",
                {
                    "action": "completed",
                    "installation": {"id": 654},
                    "repository": {"full_name": "example/project"},
                    "check_run": {"pull_requests": [{"number": 17}]},
                },
            )
            assert ci.status_code == 200
            assert ci.json()["status"] == "queued"
            assert worker.process_one()

            merged = _deliver(
                client,
                "pull_request",
                "delivery-merged",
                {
                    "action": "closed",
                    "installation": {"id": 654},
                    "number": 17,
                    "repository": {"full_name": "example/project"},
                    "pull_request": {"id": 9001, "merged": True},
                },
            )
            assert merged.status_code == 200
            assert merged.json()["status"] == "queued"
            assert worker.process_one()
            assert github.completed_items == ["PVTI_test_item"]

            replay = _deliver(
                client,
                "projects_v2_item",
                "delivery-ready",
                {
                    "action": "edited",
                    "installation": {"id": 654},
                    "projects_v2_item": {
                        "id": 71,
                        "node_id": "PVTI_test_item",
                        "project_node_id": "PVT_test_project",
                    },
                },
            )
            assert replay.status_code == 200
            assert replay.json()["status"] == "duplicate"


def test_webhook_route_rejects_bad_signature_before_graph_work(tmp_path):
    calls = []
    app = create_app(
        normalize_event=lambda *_: calls.append(True),
        settings=RuntimeSettings(
            github_webhook_secret="integration-secret",
            database_path=tmp_path / "workflow.sqlite",
        ),
    )
    body = b'{"action":"edited"}'
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-github-event": "projects_v2_item",
                "x-github-delivery": "bad-signature",
                "x-hub-signature-256": _signature(body, "wrong-secret"),
            },
        )

    assert response.status_code == 401
    assert calls == []


def test_pending_ci_blocks_a_successful_merge_event(tmp_path):
    github = FakeGitHubEventAPI(ci_status="pending", ci_complete=False)
    normalizer = GitHubEventNormalizer(
        github,
        ProjectEventConfig(),
    )
    app = create_app(
        normalize_event=normalizer,
        settings=RuntimeSettings(
            github_webhook_secret="integration-secret",
            database_path=tmp_path / "workflow.sqlite",
        ),
    )

    with TestClient(app) as client:
        store = client.app.state.internal_runtime.store
        service = _local_task_service(tmp_path, store, github)
        with service as lifecycle:
            worker = LangGraphEventWorker(
                queue=LocalGitHubQueue(store, github),
                lifecycle=lifecycle,
            )
            ready = _deliver(
                client,
                "projects_v2_item",
                "delivery-ready",
                {
                    "action": "edited",
                    "installation": {"id": 654},
                    "projects_v2_item": {
                        "id": 71,
                        "node_id": "PVTI_test_item",
                    },
                },
            )
            assert ready.json()["status"] == "queued"
            assert worker.process_one()

            ci = _deliver(
                client,
                "check_run",
                "delivery-ci",
                {
                    "action": "created",
                    "installation": {"id": 654},
                    "repository": {"full_name": "example/project"},
                    "check_run": {"pull_requests": [{"number": 17}]},
                },
            )
            assert ci.json()["status"] == "queued"
            assert worker.process_one()

            merged = _deliver(
                client,
                "pull_request",
                "delivery-merged",
                {
                    "action": "closed",
                    "installation": {"id": 654},
                    "number": 17,
                    "repository": {"full_name": "example/project"},
                    "pull_request": {"id": 9001, "merged": True},
                },
            )
            assert merged.json()["status"] == "queued"
            assert worker.process_one()

    assert github.completed_items == []


def test_github_app_factory_starts_without_workflow_operations(tmp_path, monkeypatch):
    class FakeAppClient:
        @classmethod
        def from_settings(cls, _settings):
            return cls()

        def get_project_item(self, item_node_id, installation_id):
            assert item_node_id == "PVTI_test_item"
            assert installation_id == 654
            return {
                "project_node_id": "PVT_test_project",
                "project_owner": "example-org",
                "project_owner_type": "organization",
                "status_field_name": "Status",
                "status_option_name": "Ready",
                "issue": {"repository": "example/project", "number": 42},
            }

        def close(self):
            pass

    monkeypatch.setattr("github_service.bootstrap.GitHubAppClient", FakeAppClient)
    app = create_github_app(
        settings=RuntimeSettings(
            github_webhook_secret="integration-secret",
            database_path=tmp_path / "workflow.sqlite",
            github_app_id=321,
        ),
    )

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_github_app_factory_requires_app_credentials(tmp_path):
    settings = RuntimeSettings(
        github_webhook_secret="integration-secret",
        database_path=tmp_path / "workflow.sqlite",
    )

    try:
        create_github_app(settings=settings)
    except ValueError as error:
        assert "GitHub App settings are missing" in str(error)
    else:
        raise AssertionError("GitHub App factory must require App credentials")
