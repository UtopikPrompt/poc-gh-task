from github_service.completion import GitHubTaskCompletion


class FakeStore:
    def pull_request_for_task(self, task_id):
        assert task_id == "task-1"
        return {
            "repository": "example/project",
            "number": 17,
            "pull_request_id": "9001",
        }

    def task_context(self, task_id):
        assert task_id == "task-1"
        return {
            "project_node_id": "PVT_project",
            "project_item_id": "PVTI_item",
            "target_repository": "example/project",
            "installation_id": "654",
        }


class FakeGitHub:
    def __init__(self, verified):
        self.verified = verified
        self.marked = []

    def verify_pull_request_completion(
        self, repository, number, expected_id, installation_id
    ):
        assert (repository, number, expected_id, installation_id) == (
            "example/project",
            17,
            "9001",
            654,
        )
        return {"verified": self.verified}

    def mark_task_done(self, state, installation_id):
        self.marked.append((state, installation_id))
        return {"updated": True}


def test_completion_requires_github_revalidation_before_project_write():
    github = FakeGitHub(verified=False)
    complete = GitHubTaskCompletion(
        store=FakeStore(),
        github=github,
    )

    assert complete("task-1") == {
        "completed": False,
        "reason": "merge_or_checks_unverified",
    }
    assert github.marked == []


def test_completion_updates_only_the_mapped_project_item_after_revalidation():
    github = FakeGitHub(verified=True)
    complete = GitHubTaskCompletion(
        store=FakeStore(),
        github=github,
    )

    assert complete("task-1") == {
        "completed": True,
        "project_update": {"updated": True},
    }
    assert github.marked == [
        (
            {"project_node_id": "PVT_project", "project_item_id": "PVTI_item"},
            654,
        )
    ]
