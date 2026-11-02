from contextlib import nullcontext
from hashlib import sha1

import pytest
from github_service.delivery_store import SQLiteDeliveryStore
from github_service.publishing import (
    FileChangeProposal,
    GitHubTaskPublisher,
    MAX_FILE_BYTES,
    MAX_PROPOSAL_BYTES,
    MAX_PROPOSAL_FILES,
    task_branch,
)


def _blob_sha(content):
    encoded = content.encode("utf-8")
    return sha1(b"blob " + str(len(encoded)).encode() + b"\0" + encoded).hexdigest()


class FakeGitHub:
    def __init__(self):
        self.repository = "example/project"
        self.default_branch = "main"
        self.refs = {"main": "base-commit"}
        self.commits = {"base-commit": {"tree": "base-tree", "parents": []}}
        self.trees = {
            "base-tree": {
                "README.md": {
                    "path": "README.md",
                    "mode": "100644",
                    "type": "blob",
                    "sha": _blob_sha("readme\n"),
                }
            }
        }
        self.blobs = {}
        self.pull_requests = []
        self.calls = []
        self.status = "Ready"
        self.issue_repository = self.repository
        self.next_tree = 0
        self.next_commit = 0
        self.next_pr = 0

    def get_project_item(self, item_node_id, installation_id):
        self.calls.append(("project", item_node_id, installation_id))
        return {
            "project_node_id": "PVT_project",
            "project_owner": "example-org",
            "project_owner_type": "organization",
            "is_archived": False,
            "status_option_name": self.status,
            "done_option_name": "Done",
            "issue": {"repository": self.issue_repository, "state": "OPEN"},
        }

    def get_repository(self, repository, installation_id):
        self.calls.append(("repository", repository, installation_id))
        return {
            "full_name": self.repository,
            "default_branch": self.default_branch,
        }

    def get_ref(self, repository, branch, installation_id):
        self.calls.append(("get_ref", branch))
        sha = self.refs.get(branch)
        return None if sha is None else {"object": {"sha": sha}}

    def get_commit(self, repository, sha, installation_id):
        self.calls.append(("get_commit", sha))
        commit = self.commits[sha]
        return {"tree": {"sha": commit["tree"]}}

    def get_tree(self, repository, sha, installation_id):
        self.calls.append(("get_tree", sha))
        return {
            "truncated": False,
            "tree": [dict(entry) for entry in self.trees[sha].values()],
        }

    def create_blob(self, repository, content, installation_id):
        self.calls.append(("create_blob", content))
        blob_sha = _blob_sha(content)
        self.blobs[blob_sha] = content
        return {"sha": blob_sha}

    def create_tree(self, repository, base_tree, entries, installation_id):
        self.calls.append(("create_tree", base_tree, entries))
        self.next_tree += 1
        tree_sha = f"tree-{self.next_tree}"
        tree = dict(self.trees[base_tree])
        for entry in entries:
            tree[entry["path"]] = dict(entry)
        self.trees[tree_sha] = tree
        return {"sha": tree_sha}

    def create_commit(self, repository, message, tree, parent, installation_id):
        self.calls.append(("create_commit", message, tree, parent))
        self.next_commit += 1
        commit_sha = f"commit-{self.next_commit}"
        self.commits[commit_sha] = {"tree": tree, "parents": [parent]}
        return {"sha": commit_sha}

    def create_ref(self, repository, branch, sha, installation_id):
        self.calls.append(("create_ref", branch, sha))
        assert branch not in self.refs
        self.refs[branch] = sha

    def update_ref(self, repository, branch, sha, installation_id):
        self.calls.append(("update_ref", branch, sha, False))
        parent = self.commits[sha]["parents"][0]
        assert self.refs[branch] == parent
        self.refs[branch] = sha

    def get_pull_request(self, repository, number, installation_id):
        self.calls.append(("get_pr", number))
        return next(pr for pr in self.pull_requests if pr["number"] == number)

    def find_pull_requests(self, repository, branch, base, installation_id):
        self.calls.append(("find_pr", branch, base))
        return [
            pr
            for pr in self.pull_requests
            if pr["head"] == branch and pr["base"] == base
        ]

    def create_pull_request(
        self, *, repository, title, body, head, base, installation_id
    ):
        self.calls.append(("create_pr", repository, head, base, installation_id))
        self.next_pr += 1
        result = {
            "pull_request_id": str(9000 + self.next_pr),
            "repository": repository,
            "head_repository": repository,
            "number": 16 + self.next_pr,
            "head": head,
            "base": base,
            "state": "open",
            "title": title,
            "body": body,
            "url": f"https://github.example/{repository}/pull/{16 + self.next_pr}",
        }
        self.pull_requests.append(result)
        return result


def _store(tmp_path, task_id="task-1"):
    store = SQLiteDeliveryStore(tmp_path / "github.sqlite")
    store.enqueue_events(
        f"delivery-{task_id}",
        [
            {
                "task_id": task_id,
                "event_type": "ready",
                "project_owner": "example-org",
                "project_owner_type": "organization",
                "project_node_id": "PVT_project",
                "project_item_id": "PVTI_item",
                "target_repositories": ["example/project"],
                "installation_id": 654,
            }
        ],
    )
    return store


def _proposal(*, task_id="task-1", changes, summary="", risk_summary=""):
    payload = {"task_id": task_id, "changes": [changes]}
    if summary or risk_summary:
        payload.update({"summary": summary, "risk_summary": risk_summary})
    return FileChangeProposal.from_payload(payload)


def _change(operation, path, content):
    return {"operation": operation, "path": path, "content": content}


def test_proposal_creates_deterministic_branch_and_one_pull_request(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()
    publisher = GitHubTaskPublisher(store=store, github=github)

    result = publisher.publish(
        _proposal(changes=_change("create", "src/task.py", "print('task')\n"))
    )

    branch = task_branch("task-1")
    assert github.refs["main"] == "base-commit"
    assert github.refs[branch] == "commit-1"
    assert [call[0] for call in github.calls].count("create_blob") == 1
    assert [call[0] for call in github.calls].count("create_tree") == 1
    assert [call[0] for call in github.calls].count("create_commit") == 1
    assert ("create_ref", branch, "commit-1") in github.calls
    assert ("create_pr", "example/project", branch, "main", 654) in github.calls
    assert result["pull_request_id"] == "9001"
    assert "## Advisory risk summary" not in github.pull_requests[0]["body"]
    assert store.task_for_pull_request("example/project", 17) == "task-1"
    store.close()


def test_rework_updates_same_branch_and_pull_request_and_retry_is_idempotent(
    tmp_path,
):
    store = _store(tmp_path)
    github = FakeGitHub()
    publisher = GitHubTaskPublisher(store=store, github=github)
    first = publisher.publish(
        _proposal(changes=_change("create", "src/task.py", "first\n"))
    )
    first_commit = github.refs[task_branch("task-1")]

    second_proposal = _proposal(changes=_change("update", "src/task.py", "second\n"))
    second = publisher.publish(second_proposal)
    second_commit = github.refs[task_branch("task-1")]
    calls_after_update = len(github.calls)
    repeated = publisher.publish(second_proposal)

    assert second_commit != first_commit
    assert github.commits[second_commit]["parents"] == [first_commit]
    assert ("update_ref", task_branch("task-1"), second_commit, False) in github.calls
    assert second["pull_request_id"] == first["pull_request_id"]
    assert repeated["pull_request_id"] == first["pull_request_id"]
    assert len(github.pull_requests) == 1
    assert not any(
        call[0] in {"create_blob", "create_tree", "create_commit", "update_ref"}
        for call in github.calls[calls_after_update:]
    )
    assert [call[0] for call in github.calls].count("create_commit") == 2
    store.close()


def test_missing_context_or_inactive_task_fails_before_git_writes(tmp_path):
    class NoContextStore:
        def task_context(self, _task_id):
            return None

        def publication_lock(self, _task_id):
            return nullcontext()

    github = FakeGitHub()
    proposal = _proposal(changes=_change("create", "src/task.py", "text"))
    with pytest.raises(ValueError, match="no GitHub-owned repository context"):
        GitHubTaskPublisher(store=NoContextStore(), github=github).publish(proposal)
    assert github.calls == []

    store = _store(tmp_path)
    github.status = "Done"
    with pytest.raises(ValueError, match="not verifiably active"):
        GitHubTaskPublisher(store=store, github=github).publish(proposal)
    assert [call[0] for call in github.calls] == ["project"]
    store.close()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ([_change("create", "../outside.py", "x")], "path is invalid"),
        ([_change("delete", "src/file.py", "")], "must be create or update"),
        ([_change("create", "/etc/passwd", "x")], "path is invalid"),
        ([_change("create", "src\\file.py", "x")], "path is invalid"),
        ([_change("create", ".env.local", "secret")], "path is invalid"),
        ([_change("create", "src/.docker/config.json", "secret")], "path is invalid"),
        ([_change("create", "src/file\nname.py", "x")], "path is invalid"),
        (
            [
                _change("create", "Src/file.py", "a"),
                _change("create", "src/FILE.py", "b"),
            ],
            "case-colliding",
        ),
        (
            [_change("create", "src/file.py", "x" * (MAX_FILE_BYTES + 1))],
            "at most",
        ),
        (
            [
                _change("create", f"src/{index}.py", "x" * (MAX_FILE_BYTES + 1))
                for index in range(MAX_PROPOSAL_FILES + 1)
            ],
            "at most",
        ),
    ],
)
def test_invalid_proposals_are_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        FileChangeProposal.from_payload({"task_id": "task-1", "changes": changes})


def test_proposal_total_content_limit_and_unknown_fields_are_rejected():
    changes = [
        _change("create", f"src/{index}.py", "x" * (MAX_FILE_BYTES - 1))
        for index in range(5)
    ]
    with pytest.raises(ValueError, match="proposal content"):
        FileChangeProposal.from_payload({"task_id": "task-1", "changes": changes})
    with pytest.raises(ValueError, match="unsupported fields"):
        FileChangeProposal.from_payload(
            {
                "task_id": "task-1",
                "changes": [_change("create", "a.py", "")],
                "base": "main",
            }
        )
    assert MAX_PROPOSAL_BYTES < MAX_FILE_BYTES * 5


def test_advisory_risk_summary_is_in_pr_body_without_adding_a_gate(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()
    proposal = _proposal(
        changes=_change("create", "src/task.py", "print('task')\n"),
        summary="Implement the requested task behavior.",
        risk_summary="Review caller compatibility and regression impact.",
    )

    GitHubTaskPublisher(store=store, github=github).publish(proposal)

    body = github.pull_requests[0]["body"]
    assert "Implement the requested task behavior." in body
    assert "Review caller compatibility and regression impact." in body
    assert "This summary is advisory and does not add a merge gate." in body
    store.close()


def test_base_branch_path_and_invalid_installation_are_rejected(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()
    publisher = GitHubTaskPublisher(store=store, github=github)
    with pytest.raises(ValueError, match="already exists on base branch"):
        publisher.publish(_proposal(changes=_change("create", "README.md", "new")))
    assert not any(
        call[0] in {"create_blob", "create_commit", "create_ref"}
        for call in github.calls
    )
    store.close()

    class InvalidInstallationStore:
        def task_context(self, _task_id):
            return {
                "project_node_id": "PVT_project",
                "project_item_id": "PVTI_item",
                "target_repository": "example/project",
                "installation_id": "",
            }

        def pull_request_for_task(self, _task_id):
            return None

        def publication_lock(self, _task_id):
            return nullcontext()

    github.calls.clear()
    with pytest.raises(ValueError, match="installation.id"):
        GitHubTaskPublisher(store=InvalidInstallationStore(), github=github).publish(
            _proposal(changes=_change("create", "src/a.py", "a"))
        )
    assert github.calls == []


def test_create_rejects_casefold_existing_base_path(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()

    with pytest.raises(ValueError, match="already exists on base branch"):
        GitHubTaskPublisher(store=store, github=github).publish(
            _proposal(changes=_change("create", "readme.md", "new"))
        )

    assert not any(
        call[0] in {"create_blob", "create_commit", "create_ref"}
        for call in github.calls
    )
    store.close()


def test_create_rejects_casefold_existing_task_branch_path(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()
    branch = task_branch("task-1")
    branch_tree = dict(github.trees["base-tree"])
    branch_tree["docs/Guide.md"] = {
        "path": "docs/Guide.md",
        "mode": "100644",
        "type": "blob",
        "sha": _blob_sha("branch content\n"),
    }
    github.trees["branch-tree"] = branch_tree
    github.commits["branch-commit"] = {
        "tree": "branch-tree",
        "parents": ["base-commit"],
    }
    github.refs[branch] = "branch-commit"

    with pytest.raises(ValueError, match="already exists on task branch"):
        GitHubTaskPublisher(store=store, github=github).publish(
            _proposal(changes=_change("create", "docs/guide.md", "new"))
        )

    assert not any(
        call[0] in {"create_blob", "create_tree", "create_commit", "update_ref"}
        for call in github.calls
    )
    store.close()


def test_create_rejects_exact_existing_task_branch_path_with_same_content(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()
    branch = task_branch("task-1")
    content = "existing content\n"
    branch_tree = dict(github.trees["base-tree"])
    branch_tree["src/task.py"] = {
        "path": "src/task.py",
        "mode": "100644",
        "type": "blob",
        "sha": _blob_sha(content),
    }
    github.trees["branch-tree"] = branch_tree
    github.commits["branch-commit"] = {
        "tree": "branch-tree",
        "parents": ["base-commit"],
    }
    github.refs[branch] = "branch-commit"

    with pytest.raises(ValueError, match="already exists on task branch"):
        GitHubTaskPublisher(store=store, github=github).publish(
            _proposal(changes=_change("create", "src/task.py", content))
        )

    assert not any(
        call[0] in {"create_blob", "create_tree", "create_commit", "update_ref"}
        for call in github.calls
    )
    store.close()


def test_registered_pull_request_must_match_expected_branch_and_base(tmp_path):
    store = _store(tmp_path)
    store.register_pull_request("example/project", 17, "task-1", "9001")
    github = FakeGitHub()
    github.pull_requests.append(
        {
            "pull_request_id": "9001",
            "repository": "example/project",
            "head_repository": "example/project",
            "number": 17,
            "head": "unexpected-branch",
            "base": "main",
            "state": "open",
        }
    )
    with pytest.raises(ValueError, match="does not match task branch and base"):
        GitHubTaskPublisher(store=store, github=github).publish(
            _proposal(changes=_change("create", "src/task.py", "x"))
        )
    assert not any(call[0] in {"create_blob", "create_commit"} for call in github.calls)
    store.close()


def test_existing_pull_request_mapped_to_another_task_is_not_reassigned(tmp_path):
    store = _store(tmp_path)
    github = FakeGitHub()
    branch = task_branch("task-1")
    github.pull_requests.append(
        {
            "pull_request_id": "9001",
            "repository": "example/project",
            "head_repository": "example/project",
            "number": 17,
            "head": branch,
            "base": "main",
            "state": "open",
        }
    )
    store.register_pull_request("example/project", 17, "different-task", "9001")

    with pytest.raises(ValueError, match="different task"):
        GitHubTaskPublisher(store=store, github=github).publish(
            _proposal(changes=_change("create", "src/task.py", "x"))
        )

    assert not any(
        call[0] in {"create_blob", "create_tree", "create_commit", "create_ref"}
        for call in github.calls
    )
    assert store.task_for_pull_request("example/project", 17) == "different-task"
    store.close()
