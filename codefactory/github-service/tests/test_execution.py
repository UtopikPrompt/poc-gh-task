from github_service.execution import GitHubTaskExecutionReporter


class FakeStore:
    def task_context(self, task_id):
        assert task_id == "task-1"
        return {
            "installation_id": "654",
            "project_node_id": "PVT_project",
            "project_item_id": "PVTI_item",
            "project_owner": "example-org",
            "project_owner_type": "organization",
            "target_repository": "example/project",
        }


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def mark_task_in_progress(self, state, installation_id):
        self.calls.append((state, installation_id))
        return {"updated": True}


def test_execution_reporter_uses_saved_task_mapping():
    github = FakeGitHub()
    report = GitHubTaskExecutionReporter(store=FakeStore(), github=github)

    assert report("task-1") == {
        "started": True,
        "project_update": {"updated": True},
    }
    assert github.calls == [
        (
            {
                "installation_id": "654",
                "project_node_id": "PVT_project",
                "project_item_id": "PVTI_item",
                "project_owner": "example-org",
                "project_owner_type": "organization",
                "target_repository": "example/project",
            },
            654,
        )
    ]
