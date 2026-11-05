import pytest

from github_service.conversation import GitHubTaskConversation


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
        return {"target_repository": "example/project", "installation_id": "654"}


class FakeGitHub:
    def __init__(self):
        self.comments = {}
        self.review_comments = {}
        self.created = []
        self.replies = []

    def find_issue_comment(self, repository, issue_number, marker, installation_id):
        assert (repository, issue_number, installation_id) == (
            "example/project",
            17,
            654,
        )
        return self.comments.get(marker)

    def create_issue_comment(self, repository, issue_number, body, installation_id):
        assert (repository, issue_number, installation_id) == (
            "example/project",
            17,
            654,
        )
        self.created.append(body)
        marker = body.split("\n\n")[-1]
        self.comments[marker] = str(len(self.created))
        return {"id": len(self.created)}

    def find_review_comment(
        self, repository, pull_request_number, marker, installation_id
    ):
        assert (repository, pull_request_number, installation_id) == (
            "example/project",
            17,
            654,
        )
        return self.review_comments.get(marker)

    def create_review_comment_reply(
        self, repository, pull_request_number, in_reply_to, body, installation_id
    ):
        assert (repository, pull_request_number, in_reply_to, installation_id) == (
            "example/project",
            17,
            4461111142,
            654,
        )
        self.replies.append(body)
        marker = body.split("\n\n")[-1]
        comment_id = str(100 + len(self.replies))
        self.review_comments[marker] = comment_id
        return {"id": comment_id}


def test_question_comment_is_pr_scoped_and_idempotent():
    github = FakeGitHub()
    conversation = GitHubTaskConversation(store=FakeStore(), github=github)
    dedupe_key = "a" * 64

    first = conversation.request_human_feedback(
        "task-1", "Should archived records be included?", dedupe_key
    )
    retry = conversation.request_human_feedback(
        "task-1", "Should archived records be included?", dedupe_key
    )

    assert first == {"comment_id": "1", "created": True}
    assert retry == {"comment_id": "1", "created": False}
    assert len(github.created) == 1
    assert "Should archived records be included?" in github.created[0]
    assert f"codefactory-human-feedback:{dedupe_key}" in github.created[0]


def test_question_rejects_invalid_dedupe_key():
    conversation = GitHubTaskConversation(store=FakeStore(), github=FakeGitHub())

    with pytest.raises(ValueError, match="request is invalid"):
        conversation.request_human_feedback("task-1", "Clarify this?", "not-a-key")


def test_inline_feedback_question_replies_to_source_review_comment_idempotently():
    github = FakeGitHub()
    conversation = GitHubTaskConversation(store=FakeStore(), github=github)
    dedupe_key = "b" * 64

    first = conversation.request_human_feedback(
        "task-1",
        "Should this unrelated change be reverted?",
        dedupe_key,
        source_review_comment_id=4461111142,
    )
    retry = conversation.request_human_feedback(
        "task-1",
        "Should this unrelated change be reverted?",
        dedupe_key,
        source_review_comment_id=4461111142,
    )

    assert first == {"comment_id": "101", "created": True}
    assert retry == {"comment_id": "101", "created": False}
    assert len(github.replies) == 1
    assert f"codefactory-human-feedback:{dedupe_key}" in github.replies[0]
    assert github.created == []


def test_failed_inline_feedback_reply_does_not_fall_back_to_issue_comment():
    class FailingReplyGitHub(FakeGitHub):
        def create_review_comment_reply(
            self, repository, pull_request_number, in_reply_to, body, installation_id
        ):
            raise RuntimeError("review thread is temporarily unavailable")

    github = FailingReplyGitHub()
    conversation = GitHubTaskConversation(store=FakeStore(), github=github)

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        conversation.request_human_feedback(
            "task-1",
            "Which behavior should this correction preserve?",
            "c" * 64,
            source_review_comment_id=4461111142,
        )

    assert github.created == []
    assert github.replies == []
