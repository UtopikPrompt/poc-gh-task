"""Task-scoped, bounded source context read from GitHub's current base tree."""

from __future__ import annotations

import base64
import binascii
import re
import time
import unicodedata
from collections.abc import Mapping
from typing import Protocol

from github_service.events import normalize_installation_id

MAX_SOURCE_TREE_ENTRIES = 5000
MAX_CANDIDATE_PATHS = 200
MAX_SOURCE_PATHS = 6
MAX_SOURCE_PATH_LENGTH = 240
MAX_SOURCE_FILE_BYTES = 16 * 1024
MAX_SOURCE_TOTAL_BYTES = 64 * 1024
SOURCE_READ_TIMEOUT_SECONDS = 12.0

_SAFE_EXTENSIONS = {
    ".c",
    ".cfg",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".properties",
    ".py",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SAFE_FILENAMES = {
    "dockerfile",
    "makefile",
    "readme",
    "license",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "pyproject.toml",
}
_SENSITIVE_DIRECTORY_COMPONENTS = {
    "secret",
    "secrets",
    "credential",
    "credentials",
    ".aws",
    ".azure",
    ".docker",
    ".gcloud",
    ".gnupg",
    ".kube",
    ".ssh",
}
_SECRET_PATH = re.compile(
    r"(^|/)(?:\.env(?:\..*)?|secrets?(?:\..*)?|credentials?(?:\..*)?|"
    r".*\.(?:pem|key|p12|pfx|crt|cer|der))$",
    re.IGNORECASE,
)
_SECRET_CONTENT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16})\b|\bBearer\s+[A-Za-z0-9._~+/-]{8,}|"
    r"[\"']?(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY)"
    r"[\"']?\s*[:=]",
    re.IGNORECASE,
)


class SourceContextStore(Protocol):
    def task_context(self, task_id: str) -> dict[str, str] | None: ...


class SourceContextGitHubAPI(Protocol):
    def get_repository(
        self, repository: str, installation_id: int, *, timeout: float | None = None
    ) -> Mapping[str, object]: ...

    def get_ref(
        self,
        repository: str,
        branch: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object] | None: ...

    def get_commit(
        self,
        repository: str,
        sha: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]: ...

    def get_tree(
        self,
        repository: str,
        sha: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]: ...

    def get_blob(
        self,
        repository: str,
        sha: str,
        installation_id: int,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]: ...


class GitHubTaskSource:
    def __init__(self, *, store: SourceContextStore, github: SourceContextGitHubAPI):
        self._store = store
        self._github = github

    def candidate_paths(self, task_id: str) -> dict[str, object]:
        deadline = time.monotonic() + SOURCE_READ_TIMEOUT_SECONDS
        repository, installation_id, entries = self._base_tree(task_id, deadline)
        candidates = [
            {"path": path, "size": entry["size"]}
            for path, entry in sorted(entries.items())
            if _safe_source_entry(path, entry)
        ][:MAX_CANDIDATE_PATHS]
        return {"paths": candidates}

    def read_files(self, task_id: str, paths: object) -> dict[str, object]:
        if not isinstance(paths, list) or not paths or len(paths) > MAX_SOURCE_PATHS:
            raise ValueError(f"paths must contain 1 to {MAX_SOURCE_PATHS} entries")
        if any(not isinstance(path, str) for path in paths):
            raise ValueError("each source path must be a string")
        folded_paths = [path.casefold() for path in paths]
        if len(set(folded_paths)) != len(paths):
            raise ValueError("source paths must be unique and case-distinct")

        deadline = time.monotonic() + SOURCE_READ_TIMEOUT_SECONDS
        repository, installation_id, entries = self._base_tree(task_id, deadline)
        selected_entries: list[tuple[str, Mapping[str, object]]] = []
        declared_total = 0
        for path in paths:
            if len(path) > MAX_SOURCE_PATH_LENGTH:
                raise ValueError("source path is too long")
            entry = entries.get(path)
            if entry is None or not _safe_source_path(path, entry):
                raise ValueError(
                    "source path is not a safe file in this task's base tree"
                )
            size = entry["size"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("source file has invalid size metadata")
            if size > MAX_SOURCE_FILE_BYTES:
                raise ValueError("source file exceeds the per-file byte limit")
            declared_total += size
            if declared_total > MAX_SOURCE_TOTAL_BYTES:
                raise ValueError("source files exceed the aggregate byte limit")
            selected_entries.append((path, entry))

        files: list[dict[str, str]] = []
        for path, entry in selected_entries:
            blob_sha = entry.get("sha")
            if not isinstance(blob_sha, str) or not blob_sha:
                raise ValueError("source file is missing its Git blob identity")
            blob = self._github.get_blob(
                repository,
                blob_sha,
                installation_id,
                timeout=_remaining(deadline),
            )
            content = _decode_text_blob(blob, expected_size=entry["size"])
            if _SECRET_CONTENT.search(content):
                raise ValueError("source file appears to contain secret material")
            files.append({"path": path, "content": content})
        return {"files": files}

    def _base_tree(
        self, task_id: str, deadline: float
    ) -> tuple[str, int, dict[str, Mapping[str, object]]]:
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
            raise ValueError("task_id must be a non-empty bounded string")
        context = self._store.task_context(task_id)
        if context is None:
            raise ValueError("task has no GitHub-owned repository context")
        repository = context.get("target_repository")
        if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise ValueError("task context has an invalid target repository")
        installation_id = normalize_installation_id(context.get("installation_id"))
        repository_info = self._github.get_repository(
            repository,
            installation_id,
            timeout=_remaining(deadline),
        )
        if not isinstance(repository_info.get("full_name"), str) or (
            repository_info["full_name"].casefold() != repository.casefold()
        ):
            raise ValueError("GitHub repository identity does not match task context")
        branch = repository_info.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError("GitHub repository default branch is unavailable")
        reference = self._github.get_ref(
            repository,
            branch,
            installation_id,
            timeout=_remaining(deadline),
        )
        ref_object = reference.get("object") if isinstance(reference, Mapping) else None
        commit_sha = ref_object.get("sha") if isinstance(ref_object, Mapping) else None
        if not isinstance(commit_sha, str) or not commit_sha:
            raise ValueError("GitHub repository default branch was not found")
        commit = self._github.get_commit(
            repository,
            commit_sha,
            installation_id,
            timeout=_remaining(deadline),
        )
        tree = commit.get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, Mapping) else None
        if not isinstance(tree_sha, str) or not tree_sha:
            raise ValueError("GitHub base commit is missing its tree SHA")
        response = self._github.get_tree(
            repository,
            tree_sha,
            installation_id,
            timeout=_remaining(deadline),
        )
        entries = response.get("tree")
        if response.get("truncated") is not False or not isinstance(entries, list):
            raise RuntimeError("GitHub source tree is incomplete; refusing source read")
        if len(entries) > MAX_SOURCE_TREE_ENTRIES:
            raise RuntimeError("GitHub source tree exceeds the entry limit")
        result: dict[str, Mapping[str, object]] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise TypeError("GitHub source tree contains an invalid entry")
            path = entry.get("path")
            if not isinstance(path, str) or path in result:
                raise TypeError("GitHub source tree contains an invalid path")
            result[path] = entry
        return repository, installation_id, result


def _safe_source_entry(path: str, entry: Mapping[str, object]) -> bool:
    if not _safe_source_path(path, entry):
        return False
    size = entry.get("size")
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and 0 <= size <= MAX_SOURCE_FILE_BYTES
    )


def _safe_source_path(path: str, entry: Mapping[str, object]) -> bool:
    if (
        not path
        or len(path) > MAX_SOURCE_PATH_LENGTH
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(unicodedata.category(char).startswith("C") for char in path)
        or any(
            part.casefold() in _SENSITIVE_DIRECTORY_COMPONENTS
            for part in path.split("/")[:-1]
        )
        or _SECRET_PATH.search(path)
        or entry.get("type") != "blob"
        or entry.get("mode") not in {"100644", "100755"}
    ):
        return False
    filename = path.rsplit("/", 1)[-1].casefold()
    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    return suffix in _SAFE_EXTENSIONS or filename in _SAFE_FILENAMES


def _decode_text_blob(blob: Mapping[str, object], *, expected_size: int) -> str:
    if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
        raise ValueError("GitHub source blob has an unsupported encoding")
    try:
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("GitHub source blob is not valid base64") from error
    if len(raw) != expected_size:
        raise ValueError("GitHub source blob size does not match its base tree entry")
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("GitHub source blob is not UTF-8 text") from error
    if "\0" in content or any(
        unicodedata.category(char) == "Cc" and char not in "\t\n\r" for char in content
    ):
        raise ValueError("GitHub source blob contains binary or control data")
    return content


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("GitHub source-context read exceeded its time limit")
    return remaining
