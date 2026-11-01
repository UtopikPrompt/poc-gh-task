import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from githubkit._httpx import httpx
from github_service.github_app import GitHubAppClient
from github_service.source_context import (
    MAX_CANDIDATE_PATHS,
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_PATHS,
    MAX_SOURCE_TREE_ENTRIES,
    MAX_SOURCE_TOTAL_BYTES,
    GitHubTaskSource,
)


class TaskStore:
    def task_context(self, task_id):
        assert task_id == "task-1"
        return {
            "target_repository": "example/project",
            "installation_id": "654",
        }


def _source(tmp_path, *, contents=None, entries=None, truncated=False):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "app-key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    contents = contents or {"src/task.py": b"print('task')\n"}
    entries = entries or [
        {
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": f"blob-{index}",
            "size": len(content),
        }
        for index, (path, content) in enumerate(contents.items())
    ]
    blob_by_sha = {
        f"blob-{index}": content for index, content in enumerate(contents.values())
    }
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/app/installations/654/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "installation-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        assert request.headers["Authorization"] == "token installation-token"
        if request.url.path == "/repos/example/project":
            return httpx.Response(
                200, json={"full_name": "example/project", "default_branch": "main"}
            )
        if request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "commit-sha"}})
        if request.url.path.endswith("/git/commits/commit-sha"):
            return httpx.Response(200, json={"tree": {"sha": "tree-sha"}})
        if request.url.path.endswith("/git/trees/tree-sha"):
            return httpx.Response(
                200,
                json={"truncated": truncated, "tree": entries},
            )
        if "/git/blobs/" in request.url.path:
            sha = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(blob_by_sha[sha]).decode("ascii"),
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    github = GitHubAppClient(
        app_id=321,
        private_key_path=key_path,
        project_owner="example-org",
        project_owner_type="organization",
        project_number=7,
        transport=httpx.MockTransport(handle),
    )
    return GitHubTaskSource(store=TaskStore(), github=github), github, requests


def test_candidate_tree_and_selected_files_are_task_scoped_and_safe(tmp_path):
    contents = {
        "src/task.py": b"print('task')\n",
        "src/config.py": b"CONFIG = {}\n",
        "README.md": b"readme\n",
        ".env": b"secret=value",
        "secrets.yaml": b"token: hidden",
        "secrets/config.py": b"CONFIG = {}\n",
        ".ssh/config.py": b"CONFIG = {}\n",
        ".aws/settings.py": b"CONFIG = {}\n",
        ".azure/settings.py": b"CONFIG = {}\n",
        ".config/credentials/config.py": b"CONFIG = {}\n",
        "image.png": b"\x89PNG",
    }
    entries = [
        {
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": f"blob-{index}",
            "size": len(content),
        }
        for index, (path, content) in enumerate(contents.items())
    ]
    entries.append(
        {
            "path": "src/link.py",
            "mode": "120000",
            "type": "blob",
            "sha": "symlink",
            "size": 12,
        }
    )
    entries.append(
        {
            "path": "vendor/submodule.py",
            "mode": "160000",
            "type": "commit",
            "sha": "submodule",
            "size": 1,
        }
    )
    source, github, requests = _source(tmp_path, contents=contents, entries=entries)
    try:
        candidates = source.candidate_paths("task-1")
        selected = source.read_files("task-1", ["src/task.py"])
        safe_config = source.read_files("task-1", ["src/config.py"])
        for path in (
            "secrets/config.py",
            ".ssh/config.py",
            ".aws/settings.py",
            ".azure/settings.py",
            ".config/credentials/config.py",
        ):
            with pytest.raises(ValueError, match="not a safe file"):
                source.read_files("task-1", [path])
    finally:
        github.close()

    assert candidates == {
        "paths": [
            {"path": "README.md", "size": 7},
            {"path": "src/config.py", "size": 12},
            {"path": "src/task.py", "size": 14},
        ]
    }
    assert selected == {
        "files": [{"path": "src/task.py", "content": "print('task')\n"}]
    }
    assert safe_config == {
        "files": [{"path": "src/config.py", "content": "CONFIG = {}\n"}]
    }
    assert all(
        request.url.path.startswith("/repos/example/project")
        for request in requests[1:]
    )
    assert all(
        value <= 12
        for request in requests
        for value in request.extensions["timeout"].values()
    )


@pytest.mark.parametrize("path", ["../outside.py", ".env", "missing.py"])
def test_unlisted_or_unsafe_paths_are_rejected(tmp_path, path):
    source, github, _ = _source(tmp_path)
    try:
        with pytest.raises(ValueError, match="not a safe file"):
            source.read_files("task-1", [path])
    finally:
        github.close()


def test_source_file_count_size_and_secret_material_fail_closed(tmp_path):
    source, github, _ = _source(tmp_path)
    try:
        with pytest.raises(ValueError, match="1 to"):
            source.read_files("task-1", ["src/task.py"] * (MAX_SOURCE_PATHS + 1))
    finally:
        github.close()

    large_contents = {
        f"src/{index}.py": b"x" * (MAX_SOURCE_FILE_BYTES + 1) for index in range(1)
    }
    large_entries = [
        {
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": "large-blob",
            "size": len(content),
        }
        for path, content in large_contents.items()
    ]
    source, github, _ = _source(
        tmp_path, contents=large_contents, entries=large_entries
    )
    try:
        with pytest.raises(ValueError, match="per-file byte limit"):
            source.read_files("task-1", ["src/0.py"])
    finally:
        github.close()

    for secret_content in (
        b"API_TOKEN=very-secret-value",
        b'{"password": "secret phrase with spaces"}',
    ):
        source, github, _ = _source(
            tmp_path,
            contents={"src/task.py": secret_content},
        )
        try:
            with pytest.raises(ValueError, match="secret material"):
                source.read_files("task-1", ["src/task.py"])
        finally:
            github.close()


def test_binary_source_content_is_rejected(tmp_path):
    source, github, _ = _source(tmp_path, contents={"src/task.py": b"text\x00binary"})
    try:
        with pytest.raises(ValueError, match="binary or control"):
            source.read_files("task-1", ["src/task.py"])
    finally:
        github.close()


def test_aggregate_bound_and_truncated_tree_are_rejected(tmp_path):
    contents = {f"src/{index}.py": b"x" * 12_000 for index in range(6)}
    source, github, requests = _source(tmp_path, contents=contents)
    try:
        with pytest.raises(ValueError, match="aggregate byte limit"):
            source.read_files("task-1", list(contents))
    finally:
        github.close()
    assert not any("/git/blobs/" in request.url.path for request in requests)
    assert MAX_SOURCE_TOTAL_BYTES == 64 * 1024

    source, github, _ = _source(tmp_path, truncated=True)
    try:
        with pytest.raises(RuntimeError, match="incomplete"):
            source.candidate_paths("task-1")
    finally:
        github.close()


def test_candidate_and_tree_counts_and_path_length_are_bounded(tmp_path):
    contents = {f"src/{index:03}.py": b"" for index in range(MAX_CANDIDATE_PATHS + 5)}
    source, github, _ = _source(tmp_path, contents=contents)
    try:
        assert len(source.candidate_paths("task-1")["paths"]) == MAX_CANDIDATE_PATHS
    finally:
        github.close()

    entries = [
        {
            "path": f"src/{index}.txt",
            "mode": "100644",
            "type": "blob",
            "sha": f"blob-{index}",
            "size": 0,
        }
        for index in range(MAX_SOURCE_TREE_ENTRIES + 1)
    ]
    source, github, _ = _source(tmp_path, entries=entries)
    try:
        with pytest.raises(RuntimeError, match="entry limit"):
            source.candidate_paths("task-1")
    finally:
        github.close()

    long_path = f"src/{'a' * 240}.py"
    source, github, _ = _source(tmp_path, contents={long_path: b""})
    try:
        with pytest.raises(ValueError, match="too long"):
            source.read_files("task-1", [long_path])
    finally:
        github.close()
