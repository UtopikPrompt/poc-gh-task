"""GitHub-owned, repository-bounded pull request publication capability."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Literal, Protocol
from dataclasses import dataclass
import hashlib
import re
import unicodedata

from github_service.events import normalize_installation_id

MAX_PROPOSAL_FILES = 20
MAX_FILE_BYTES = 64 * 1024
MAX_PROPOSAL_BYTES = 256 * 1024
MAX_PATH_LENGTH = 240

FileOperation = Literal["create", "update"]


@dataclass(frozen=True)
class FileChange:
    operation: FileOperation
    path: str
    content: str


@dataclass(frozen=True)
class FileChangeProposal:
    task_id: str
    changes: tuple[FileChange, ...]
    summary: str = ""
    risk_summary: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> FileChangeProposal:
        if (
            not isinstance(payload, Mapping)
            or not {"task_id", "changes"}.issubset(payload)
            or set(payload) - {"task_id", "changes", "summary", "risk_summary"}
        ):
            raise ValueError(
                "proposal contains unsupported fields or misses required fields"
            )
        task_id = payload.get("task_id")
        changes = payload.get("changes")
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or len(task_id) > 256
            or _has_control_characters(task_id)
        ):
            raise ValueError("task_id must be a non-empty bounded string")
        if not isinstance(changes, list) or not changes:
            raise ValueError("changes must be a non-empty array")
        summary = payload.get("summary", "")
        risk_summary = payload.get("risk_summary", "")
        summary_fields_present = "summary" in payload or "risk_summary" in payload
        if (
            not isinstance(summary, str)
            or len(summary) > 1200
            or _has_control_characters(summary)
            or not isinstance(risk_summary, str)
            or len(risk_summary) > 1600
            or _has_control_characters(risk_summary)
            or bool(summary.strip()) != bool(risk_summary.strip())
            or (
                summary_fields_present
                and (not summary.strip() or not risk_summary.strip())
            )
        ):
            raise ValueError("proposal summary fields must be bounded paired text")
        if len(changes) > MAX_PROPOSAL_FILES:
            raise ValueError(f"proposal may contain at most {MAX_PROPOSAL_FILES} files")

        parsed: list[FileChange] = []
        paths: set[str] = set()
        total_bytes = 0
        for item in changes:
            if not isinstance(item, Mapping) or set(item) != {
                "operation",
                "path",
                "content",
            }:
                raise ValueError(
                    "each change must contain only operation, path, and content"
                )
            operation = item.get("operation")
            path = item.get("path")
            content = item.get("content")
            if not isinstance(operation, str) or operation not in {"create", "update"}:
                raise ValueError("file operation must be create or update")
            if not isinstance(path, str) or not _valid_file_path(path):
                raise ValueError("file path is invalid or sensitive")
            folded_path = path.casefold()
            if folded_path in paths:
                raise ValueError("proposal contains duplicate or case-colliding paths")
            paths.add(folded_path)
            if not isinstance(content, str) or "\0" in content:
                raise ValueError("file content must be text without NUL characters")
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > MAX_FILE_BYTES:
                raise ValueError(f"each file must be at most {MAX_FILE_BYTES} bytes")
            total_bytes += content_bytes
            if total_bytes > MAX_PROPOSAL_BYTES:
                raise ValueError(
                    f"proposal content must be at most {MAX_PROPOSAL_BYTES} bytes"
                )
            parsed.append(FileChange(operation, path, content))
        return cls(task_id, tuple(parsed), summary, risk_summary)


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _valid_file_path(path: str) -> bool:
    if (
        not path
        or len(path) > MAX_PATH_LENGTH
        or path.startswith("/")
        or "\\" in path
        or _has_control_characters(path)
        or re.match(r"^[A-Za-z]:", path)
    ):
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    filename = parts[-1].casefold()
    normalized_path = path.casefold()
    if (
        filename == ".env"
        or filename.startswith(".env.")
        or filename.endswith(".env")
        or filename.endswith((".pem", ".key", ".p12", ".pfx"))
        or normalized_path == ".docker/config.json"
        or normalized_path.endswith("/.docker/config.json")
        or filename
        in {
            ".netrc",
            ".npmrc",
            ".pypirc",
            "credentials",
            "credentials.json",
            "id_ed25519",
            "id_rsa",
            "kubeconfig",
            "private-key",
            "private_key",
            "secrets.env",
            "service-account.json",
        }
        or filename.startswith(("credentials.", "service-account."))
        or filename in {"secrets.json", "secrets.toml", "secrets.yaml", "secrets.yml"}
    ):
        return False
    return True


def validate_proposal(proposal: FileChangeProposal) -> FileChangeProposal:
    if not isinstance(proposal, FileChangeProposal):
        raise ValueError("proposal must be a typed file-change proposal")
    payload: dict[str, object] = {
        "task_id": proposal.task_id,
        "changes": [
            {
                "operation": change.operation,
                "path": change.path,
                "content": change.content,
            }
            for change in proposal.changes
        ],
    }
    if proposal.summary or proposal.risk_summary:
        payload["summary"] = proposal.summary
        payload["risk_summary"] = proposal.risk_summary
    return FileChangeProposal.from_payload(payload)


class PublishingStore(Protocol):
    def task_context(self, task_id: str) -> dict[str, str] | None: ...

    def pull_request_for_task(self, task_id: str) -> dict[str, object] | None: ...

    def task_for_pull_request(
        self, repository: str, pull_request_number: int
    ) -> str | None: ...

    def publication_lock(self, task_id: str) -> AbstractContextManager[None]: ...

    def register_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        task_id: str,
        pull_request_id: str | None = None,
    ) -> None: ...


class ProposalGitHubAPI(Protocol):
    def get_project_item(
        self, item_node_id: str, installation_id: int
    ) -> Mapping[str, object]: ...

    def get_repository(
        self, repository: str, installation_id: int
    ) -> Mapping[str, object]: ...

    def get_ref(
        self, repository: str, branch: str, installation_id: int
    ) -> Mapping[str, object] | None: ...

    def get_commit(
        self, repository: str, sha: str, installation_id: int
    ) -> Mapping[str, object]: ...

    def get_tree(
        self, repository: str, sha: str, installation_id: int
    ) -> Mapping[str, object]: ...

    def create_blob(
        self, repository: str, content: str, installation_id: int
    ) -> Mapping[str, object]: ...

    def create_tree(
        self,
        repository: str,
        base_tree: str,
        entries: list[Mapping[str, object]],
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def create_commit(
        self,
        repository: str,
        message: str,
        tree: str,
        parent: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def create_ref(
        self, repository: str, branch: str, sha: str, installation_id: int
    ) -> None: ...

    def update_ref(
        self, repository: str, branch: str, sha: str, installation_id: int
    ) -> None: ...

    def get_pull_request(
        self, repository: str, number: int, installation_id: int
    ) -> Mapping[str, object]: ...

    def find_pull_requests(
        self, repository: str, branch: str, base: str, installation_id: int
    ) -> list[Mapping[str, object]]: ...

    def create_pull_request(
        self,
        *,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def update_pull_request_body(
        self,
        repository: str,
        number: int,
        body: str,
        installation_id: int,
    ) -> Mapping[str, object]: ...

    def get_pull_request_ci(
        self,
        repository: str,
        pull_request_number: int,
        installation_id: int,
    ) -> Mapping[str, object]: ...


class GitHubTaskPublisher:
    def __init__(self, *, store: PublishingStore, github: ProposalGitHubAPI):
        self._store = store
        self._github = github

    def publish(self, proposal: FileChangeProposal) -> dict[str, object]:
        proposal = validate_proposal(proposal)
        with self._store.publication_lock(proposal.task_id):
            return self._publish_locked(proposal)

    def _publish_locked(self, proposal: FileChangeProposal) -> dict[str, object]:
        context = self._store.task_context(proposal.task_id)
        if context is None:
            raise ValueError("task has no GitHub-owned repository context")
        repository = context.get("target_repository")
        if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise ValueError("task context has an invalid target repository")
        installation_id = normalize_installation_id(context.get("installation_id"))
        issue_number = self._verify_active_task(context, repository, installation_id)

        repository_info = self._github.get_repository(repository, installation_id)
        if not _same_repository(repository_info.get("full_name"), repository):
            raise ValueError("GitHub repository identity does not match task context")
        base = repository_info.get("default_branch")
        if not isinstance(base, str) or not base.strip():
            raise ValueError("GitHub repository default branch is unavailable")
        branch = task_branch(proposal.task_id)
        if branch == base:
            raise ValueError("task branch must not be the repository default branch")

        base_ref = self._github.get_ref(repository, base, installation_id)
        if base_ref is None:
            raise ValueError("GitHub repository default branch was not found")
        base_sha = _ref_sha(base_ref)
        base_commit = self._github.get_commit(repository, base_sha, installation_id)
        base_tree_sha = _commit_tree_sha(base_commit)
        base_entries = _tree_entries(
            self._github.get_tree(repository, base_tree_sha, installation_id)
        )

        branch_ref = self._github.get_ref(repository, branch, installation_id)
        if branch_ref is None:
            parent_sha = base_sha
            current_tree_sha = base_tree_sha
            current_entries = base_entries
        else:
            parent_sha = _ref_sha(branch_ref)
            current_commit = self._github.get_commit(
                repository, parent_sha, installation_id
            )
            current_tree_sha = _commit_tree_sha(current_commit)
            current_entries = _tree_entries(
                self._github.get_tree(repository, current_tree_sha, installation_id)
            )

        desired = {
            change.path: (change, _blob_sha(change.content))
            for change in proposal.changes
        }
        changed: list[tuple[FileChange, str]] = []
        for path, (change, content_sha) in desired.items():
            current_entry = current_entries.get(path)
            if change.operation == "create":
                if any(
                    existing_path.casefold() == path.casefold()
                    for existing_path in base_entries
                ):
                    raise ValueError(
                        f"create path already exists on base branch: {path}"
                    )
                if any(
                    existing_path != path
                    and existing_path.casefold() == path.casefold()
                    for existing_path in current_entries
                ):
                    raise ValueError(
                        f"create path already exists on task branch: {path}"
                    )
                if current_entry is not None:
                    raise ValueError(
                        f"create path already exists on task branch: {path}"
                    )
            elif (
                current_entry is None
                or current_entry.get("type") != "blob"
                or current_entry.get("mode") not in {"100644", "100755"}
            ):
                raise ValueError(f"update path does not name a regular file: {path}")
            if current_entry is None or current_entry.get("sha") != content_sha:
                changed.append((change, content_sha))

        if not changed and branch_ref is None:
            raise ValueError("proposal produces no changes against the base branch")

        branch_head_sha = parent_sha
        existing = self._existing_pull_request(
            proposal.task_id,
            repository,
            branch,
            base,
            installation_id,
        )
        if changed:
            tree_entries = []
            for change, _ in changed:
                blob = self._github.create_blob(
                    repository, change.content, installation_id
                )
                blob_sha = blob.get("sha")
                if not isinstance(blob_sha, str) or not blob_sha:
                    raise TypeError("GitHub did not return a valid blob identity")
                current_entry = current_entries.get(change.path)
                mode = (
                    current_entry.get("mode") if current_entry is not None else "100644"
                )
                tree_entries.append(
                    {
                        "path": change.path,
                        "mode": mode,
                        "type": "blob",
                        "sha": blob_sha,
                    }
                )
            tree = self._github.create_tree(
                repository,
                current_tree_sha,
                tree_entries,
                installation_id,
            )
            tree_sha = tree.get("sha")
            if not isinstance(tree_sha, str) or not tree_sha:
                raise TypeError("GitHub did not return a valid tree identity")
            commit = self._github.create_commit(
                repository,
                f"Apply task proposal {proposal.task_id[:64]}",
                tree_sha,
                parent_sha,
                installation_id,
            )
            commit_sha = commit.get("sha")
            if not isinstance(commit_sha, str) or not commit_sha:
                raise TypeError("GitHub did not return a valid commit identity")
            branch_head_sha = commit_sha
            if branch_ref is None:
                self._github.create_ref(repository, branch, commit_sha, installation_id)
            else:
                self._github.update_ref(repository, branch, commit_sha, installation_id)

        issue_reference = f"Closes {repository}#{issue_number}"
        if existing is not None:
            result = existing
            existing_body = result.get("body")
            if existing_body is None:
                existing_body = ""
            elif not isinstance(existing_body, str):
                raise TypeError("GitHub did not return the pull request body")
            if issue_reference not in existing_body.splitlines():
                updated_body = f"{existing_body.rstrip()}\n\n{issue_reference}".lstrip()
                result = self._github.update_pull_request_body(
                    repository,
                    result["number"],
                    updated_body,
                    installation_id,
                )
                result = self._verify_pull_request(
                    result,
                    repository,
                    branch,
                    base,
                    expected_id=existing.get("pull_request_id"),
                )
        else:
            body = "This pull request contains a bounded file-change proposal."
            if proposal.summary:
                body += f"\n\n## Summary\n\n{proposal.summary}"
                body += (
                    "\n\n## Advisory risk summary\n\n"
                    f"{proposal.risk_summary}\n\n"
                    "This summary is advisory and does not add a merge gate."
                )
            body += f"\n\n{issue_reference}"
            result = self._github.create_pull_request(
                repository=repository,
                title="Automated changes for project task",
                body=body,
                head=branch,
                base=base,
                installation_id=installation_id,
            )
            self._verify_pull_request(
                result,
                repository,
                branch,
                base,
                expected_id=None,
            )
        number = result.get("number")
        pull_request_id = result.get("pull_request_id")
        if not isinstance(number, int) or isinstance(number, bool):
            raise TypeError("GitHub did not return a valid task pull request number")
        if not isinstance(pull_request_id, str) or not pull_request_id:
            raise TypeError("GitHub did not return a valid task pull request identity")
        mapped_task = self._store.task_for_pull_request(repository, number)
        if mapped_task is not None and mapped_task != proposal.task_id:
            raise ValueError("pull request is already mapped to a different task")
        self._store.register_pull_request(
            repository,
            number,
            proposal.task_id,
            pull_request_id,
        )
        ci = self._github.get_pull_request_ci(repository, number, installation_id)
        checks = ci.get("ci_checks")
        ci_complete = ci.get("ci_complete")
        if (
            str(ci.get("pull_request_id")) != pull_request_id
            or not isinstance(checks, list)
            or any(not isinstance(check, Mapping) for check in checks)
            or not isinstance(ci_complete, bool)
        ):
            raise TypeError("GitHub returned an invalid pull request CI snapshot")
        return {
            **dict(result),
            "head_sha": branch_head_sha,
            "ci_head_sha": ci.get("head_sha")
            if isinstance(ci.get("head_sha"), str)
            else None,
            "ci_checks": [dict(check) for check in checks],
            "ci_complete": ci_complete,
        }

    def _verify_active_task(
        self,
        context: Mapping[str, str],
        repository: str,
        installation_id: int,
    ) -> int:
        item_id = context.get("project_item_id")
        project_id = context.get("project_node_id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("task context is missing its GitHub Project item")
        item = self._github.get_project_item(item_id, installation_id)
        issue = item.get("issue")
        if (
            item.get("project_node_id") != project_id
            or item.get("project_owner") != context.get("project_owner")
            or item.get("project_owner_type") != context.get("project_owner_type")
            or item.get("is_archived") is not False
            or not isinstance(issue, Mapping)
            or issue.get("state") != "OPEN"
            or not _same_repository(issue.get("repository"), repository)
        ):
            raise ValueError("GitHub Project task no longer matches task context")
        issue_number = issue.get("number")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number <= 0
        ):
            raise ValueError("GitHub Project task has an invalid issue number")
        status = item.get("status_option_name")
        done_status = item.get("done_option_name")
        if (
            not isinstance(status, str)
            or not status
            or not isinstance(done_status, str)
            or status == done_status
        ):
            raise ValueError("GitHub Project task is not verifiably active")
        return issue_number

    def _existing_pull_request(
        self,
        task_id: str,
        repository: str,
        branch: str,
        base: str,
        installation_id: int,
    ) -> Mapping[str, object] | None:
        registered = self._store.pull_request_for_task(task_id)
        if registered is not None:
            registered_repository = registered.get("repository")
            number = registered.get("number")
            pull_request_id = registered.get("pull_request_id")
            if (
                not _same_repository(registered_repository, repository)
                or not isinstance(number, int)
                or isinstance(number, bool)
                or not isinstance(pull_request_id, str)
                or not pull_request_id
            ):
                raise ValueError("registered pull request has invalid task identity")
            mapped_task = self._store.task_for_pull_request(repository, number)
            if mapped_task != task_id:
                raise ValueError("registered pull request belongs to a different task")
            result = self._github.get_pull_request(repository, number, installation_id)
            return self._verify_pull_request(
                result,
                repository,
                branch,
                base,
                expected_id=pull_request_id,
            )

        matches = self._github.find_pull_requests(
            repository, branch, base, installation_id
        )
        if len(matches) > 1:
            raise ValueError("multiple pull requests exist for the task branch")
        if not matches:
            return None
        existing = self._verify_pull_request(
            matches[0], repository, branch, base, expected_id=None
        )
        number = existing.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise TypeError("GitHub returned an invalid existing pull request number")
        mapped_task = self._store.task_for_pull_request(repository, number)
        if mapped_task is not None and mapped_task != task_id:
            raise ValueError("pull request is already mapped to a different task")
        return existing

    @staticmethod
    def _verify_pull_request(
        pull_request: Mapping[str, object],
        repository: str,
        branch: str,
        base: str,
        *,
        expected_id: str | None,
    ) -> Mapping[str, object]:
        if (
            not _same_repository(pull_request.get("repository"), repository)
            or not _same_repository(pull_request.get("head_repository"), repository)
            or pull_request.get("head") != branch
            or pull_request.get("base") != base
            or pull_request.get("state") != "open"
            or not isinstance(pull_request.get("pull_request_id"), str)
            or not pull_request.get("pull_request_id")
            or (
                expected_id is not None
                and pull_request.get("pull_request_id") != expected_id
            )
        ):
            raise ValueError(
                "existing pull request does not match task branch and base"
            )
        return pull_request


def task_branch(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    return f"codefactory-task-{digest}"


def _same_repository(value: object, expected: str) -> bool:
    return isinstance(value, str) and value.casefold() == expected.casefold()


def _ref_sha(reference: Mapping[str, object]) -> str:
    ref_object = reference.get("object")
    sha = ref_object.get("sha") if isinstance(ref_object, Mapping) else None
    if not isinstance(sha, str) or not sha:
        raise TypeError("GitHub ref is missing its commit SHA")
    return sha


def _commit_tree_sha(commit: Mapping[str, object]) -> str:
    tree = commit.get("tree")
    sha = tree.get("sha") if isinstance(tree, Mapping) else None
    if not isinstance(sha, str) or not sha:
        raise TypeError("GitHub commit is missing its tree SHA")
    return sha


def _tree_entries(tree: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    entries = tree.get("tree")
    if tree.get("truncated") is not False or not isinstance(entries, list):
        raise RuntimeError("GitHub tree is incomplete; refusing to apply proposal")
    result: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError("GitHub tree contains an invalid entry")
        path = entry.get("path")
        if not isinstance(path, str) or path in result:
            raise TypeError("GitHub tree contains an invalid or duplicate path")
        result[path] = entry
    return result


def _blob_sha(content: str) -> str:
    encoded = content.encode("utf-8")
    return hashlib.sha1(
        b"blob " + str(len(encoded)).encode() + b"\0" + encoded
    ).hexdigest()
