from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier

import pytest

from codefactory.github_service.delivery_store import SQLiteDeliveryStore
from codefactory.github_service.publishing import GitHubTaskPublisher


class FakeGitHub:
    def __init__(self):
        self.requests = []

    def create_pull_request(self, **request):
        self.requests.append(request)
        return {
            "pull_request_id": "9001",
            "repository": request["repository"],
            "number": 17,
            "url": "https://github.example/org/repo/pull/17",
        }


def test_pr_publication_uses_github_owned_target_repository(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    store.enqueue_events(
        "delivery-1",
        [
            {
                "task_id": "task-1",
                "event_type": "ready",
                "project_node_id": "PVT_project",
                "project_item_id": "PVTI_item",
                "target_repositories": ["example/project"],
            }
        ],
    )
    github = FakeGitHub()
    publisher = GitHubTaskPublisher(store=store, github=github)

    result = publisher.publish(
        task_id="task-1",
        title="Implement task",
        body="Risk summary and validation details",
        head="automation/task-1",
        base="main",
    )

    assert github.requests == [
        {
            "repository": "example/project",
            "title": "Implement task",
            "body": "Risk summary and validation details",
            "head": "automation/task-1",
            "base": "main",
        }
    ]
    assert result["pull_request_id"] == "9001"
    assert store.task_for_pull_request("example/project", 17) == "task-1"
    assert store.pull_request_for_task("task-1")["pull_request_id"] == "9001"
    repeated = publisher.publish(
        task_id="task-1",
        title="Implement task",
        body="Risk summary and validation details",
        head="automation/task-1",
        base="main",
    )
    assert repeated["pull_request_id"] == "9001"
    assert len(github.requests) == 1
    store.close()


def test_pr_publication_rejects_task_without_github_owned_context():
    class NoContextStore:
        def task_context(self, _task_id):
            return None

        def publication_lock(self, _task_id):
            return nullcontext()

    github = FakeGitHub()
    publisher = GitHubTaskPublisher(store=NoContextStore(), github=github)

    with pytest.raises(ValueError, match="no GitHub-owned repository context"):
        publisher.publish(
            task_id="task-1",
            title="Implement task",
            body="Risk summary",
            head="automation/task-1",
            base="main",
        )
    assert github.requests == []


def test_concurrent_pr_publication_creates_only_one_pull_request(tmp_path):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    store.enqueue_events(
        "delivery-concurrent",
        [
            {
                "task_id": "task-concurrent",
                "event_type": "ready",
                "project_node_id": "PVT_project",
                "project_item_id": "PVTI_item",
                "target_repositories": ["example/project"],
            }
        ],
    )
    github = FakeGitHub()
    publisher = GitHubTaskPublisher(store=store, github=github)
    start = Barrier(3)

    def publish():
        start.wait()
        return publisher.publish(
            task_id="task-concurrent",
            title="Implement task",
            body="Risk summary",
            head="automation/task-concurrent",
            base="main",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish) for _ in range(2)]
        start.wait()
        results = [future.result() for future in futures]

    assert len(github.requests) == 1
    assert [result["pull_request_id"] for result in results] == ["9001", "9001"]
    store.close()
