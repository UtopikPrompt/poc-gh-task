import hashlib
from pathlib import Path

import httpx
import pytest

from langgraph_service.model_download import (
    MAX_ARTIFACT_BYTES,
    ModelDownloadError,
    _public_addresses,
    download_model_artifact,
    resolve_huggingface_model_file,
    validate_download_url,
    validate_model_filename,
    validate_sha256,
)


def test_resolves_exact_file_lfs_oid_for_show_file_info_url():
    revision = "b" * 40
    digest = "a" * 64
    seen = []

    def metadata_response(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/api/models/ornith-ai/model-repo"):
            return httpx.Response(200, json={"sha": revision})
        return httpx.Response(
            200,
            json=[
                {
                    "type": "file",
                    "path": "another-model.gguf",
                    "lfs": {"oid": "sha256:" + "c" * 64},
                },
                {
                    "type": "file",
                    "path": "model-Q4_K_M.gguf",
                    "lfs": {"oid": "sha256:" + digest},
                },
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(metadata_response)) as client:
        result = resolve_huggingface_model_file(
            "https://huggingface.co/ornith-ai/model-repo"
            "?show_file_info=model-Q4_K_M.gguf",
            client=client,
        )

    assert result == (
        "https://huggingface.co/ornith-ai/model-repo/resolve/"
        f"{revision}/model-Q4_K_M.gguf?download=true",
        digest,
    )
    assert seen == [
        "/api/models/ornith-ai/model-repo",
        f"/api/models/ornith-ai/model-repo/tree/{revision}",
    ]


@pytest.mark.parametrize("lfs", [{}, {"oid": "sha256:" + "z" * 64}, None])
def test_rejects_missing_or_invalid_lfs_sha(lfs):
    revision = "b" * 40

    def metadata_response(request):
        if request.url.path.endswith("/api/models/ornith-ai/model-repo"):
            return httpx.Response(200, json={"sha": revision})
        return httpx.Response(
            200,
            json=[{"type": "file", "path": "model.gguf", "lfs": lfs}],
        )

    with httpx.Client(transport=httpx.MockTransport(metadata_response)) as client:
        with pytest.raises(ModelDownloadError, match="metadata could not be verified"):
            resolve_huggingface_model_file(
                "https://huggingface.co/ornith-ai/model-repo?show_file_info=model.gguf",
                client=client,
            )


def test_rejects_metadata_without_an_exact_file_match():
    revision = "b" * 40

    def metadata_response(request):
        if request.url.path.endswith("/api/models/ornith-ai/model-repo"):
            return httpx.Response(200, json={"sha": revision})
        return httpx.Response(
            200,
            json=[{"type": "file", "path": "other.gguf", "lfs": {"oid": "a" * 64}}],
        )

    with httpx.Client(transport=httpx.MockTransport(metadata_response)) as client:
        with pytest.raises(ModelDownloadError, match="metadata could not be verified"):
            resolve_huggingface_model_file(
                "https://huggingface.co/ornith-ai/model-repo?show_file_info=model.gguf",
                client=client,
            )


@pytest.mark.parametrize(
    "url",
    [
        "https://huggingface.co/ornith-ai/model-repo",
        "https://huggingface.co/ornith-ai/model-repo?show_file_info=",
        "https://huggingface.co/ornith-ai/model-repo/model.gguf"
        "?show_file_info=other.gguf",
        "https://example.com/ornith-ai/model-repo?show_file_info=model.gguf",
        "https://huggingface.co/ornith-ai/model-repo/README.md",
    ],
)
def test_rejects_malformed_or_unsupported_model_file_urls(url):
    with pytest.raises(ValueError):
        resolve_huggingface_model_file(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://huggingface.co/org/repo/resolve/main/model.gguf?download=true",
        "https://huggingface.co/org/repo/blob/main/model.gguf",
    ],
)
def test_resolves_existing_file_url_formats(url):
    digest = "a" * 64

    def metadata_response(request):
        assert request.url.path.endswith("/tree/main")
        return httpx.Response(
            200,
            json=[{"type": "file", "path": "model.gguf", "lfs": {"oid": digest}}],
        )

    with httpx.Client(transport=httpx.MockTransport(metadata_response)) as client:
        download_url, resolved_digest = resolve_huggingface_model_file(
            url, client=client
        )

    assert download_url == (
        "https://huggingface.co/org/repo/resolve/main/model.gguf?download=true"
    )
    assert resolved_digest == digest


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/model/file.gguf",
        "https://www.huggingface.co/model/file.gguf",
        "https://user:pass@huggingface.co/model/file.gguf",
        "https://huggingface.co/model/file.gguf?access_token=secret-value",
        "https://huggingface.co:8443/model/file.gguf",
        "https://huggingface.co/model/file.gguf#fragment",
    ],
)
def test_download_url_requires_canonical_credential_free_https(url):
    with pytest.raises(ValueError):
        validate_download_url(url)


@pytest.mark.parametrize(
    "filename", ["../model.gguf", ".model.gguf", "model.bin", "a/b.gguf"]
)
def test_model_filename_is_a_safe_metadata_basename(filename):
    with pytest.raises(ValueError):
        validate_model_filename(filename)


def test_sha256_is_required_and_normalized():
    expected = "A" * 64
    assert validate_sha256(expected) == expected.lower()
    with pytest.raises(ValueError):
        validate_sha256("a" * 63)


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "127.0.0.1", "10.1.2.3", "169.254.1.1", "224.0.0.1", "::1", "fc00::1"],
)
def test_dns_rejects_non_public_destinations(monkeypatch, address):
    monkeypatch.setattr(
        "langgraph_service.model_download.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", (address, 443))],
    )
    with pytest.raises(ModelDownloadError):
        _public_addresses("huggingface.co")


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.chunks = iter(chunks)

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, _size):
        return next(self.chunks, b"")


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.request_args = None
        self.closed = False

    def request(self, method, target, headers):
        self.request_args = (method, target, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_streams_hashes_and_atomically_publishes_without_socket(tmp_path):
    artifact = b"model bytes"
    response = FakeResponse(
        headers={"Content-Length": str(len(artifact))}, chunks=[artifact]
    )
    connections = []

    def make_connection(host, address, **_kwargs):
        connection = FakeConnection(response)
        connections.append((host, address, connection))
        return connection

    verifying = []
    destination = download_model_artifact(
        url="https://huggingface.co/org/repo/resolve/main/model.gguf?download=true",
        sha256=hashlib.sha256(artifact).hexdigest(),
        filename="model.gguf",
        model_directory=tmp_path,
        on_verifying=lambda: verifying.append(True),
        resolver=lambda _host: ["8.8.8.8"],
        connection_factory=make_connection,
        required_group_id=tmp_path.stat().st_gid,
    )

    assert destination == tmp_path / "model.gguf"
    assert destination.read_bytes() == artifact
    assert verifying == [True]
    assert connections[0][2].request_args[1] == (
        "/org/repo/resolve/main/model.gguf?download=true"
    )
    assert connections[0][2].closed
    assert list(Path(tmp_path).glob(".model-download-*.tmp")) == []


def test_rejects_redirect_to_non_public_dns_before_connecting(tmp_path):
    responses = [
        FakeResponse(
            status=302, headers={"Location": "https://cdn-lfs.huggingface.co/model"}
        )
    ]
    connection_count = []

    def make_connection(*_args, **_kwargs):
        connection_count.append(True)
        return FakeConnection(responses.pop(0))

    def resolver(host):
        if host == "cdn-lfs.huggingface.co":
            raise ModelDownloadError("model host resolved to a prohibited address")
        return ["8.8.8.8"]

    with pytest.raises(ModelDownloadError, match="prohibited"):
        download_model_artifact(
            url="https://huggingface.co/org/repo/file.gguf",
            sha256="a" * 64,
            filename="model.gguf",
            model_directory=tmp_path,
            resolver=resolver,
            connection_factory=make_connection,
            required_group_id=tmp_path.stat().st_gid,
        )

    assert len(connection_count) == 1
    assert list(tmp_path.iterdir()) == []


def test_rejects_stream_larger_than_limit_and_removes_partial(tmp_path):
    response = FakeResponse(chunks=[b"1234", b"5678"])
    with pytest.raises(ModelDownloadError, match="size limit"):
        download_model_artifact(
            url="https://huggingface.co/org/repo/file.gguf",
            sha256="a" * 64,
            filename="model.gguf",
            model_directory=tmp_path,
            resolver=lambda _host: ["8.8.8.8"],
            connection_factory=lambda *_args, **_kwargs: FakeConnection(response),
            max_bytes=5,
            required_group_id=tmp_path.stat().st_gid,
        )
    assert list(tmp_path.iterdir()) == []


def test_hash_mismatch_never_publishes_and_enters_verification(tmp_path):
    response = FakeResponse(chunks=[b"unexpected"])
    verifying = []
    with pytest.raises(ModelDownloadError, match="SHA-256"):
        download_model_artifact(
            url="https://huggingface.co/org/repo/file.gguf",
            sha256="a" * 64,
            filename="model.gguf",
            model_directory=tmp_path,
            on_verifying=lambda: verifying.append(True),
            resolver=lambda _host: ["8.8.8.8"],
            connection_factory=lambda *_args, **_kwargs: FakeConnection(response),
            required_group_id=tmp_path.stat().st_gid,
        )
    assert verifying == [True]
    assert list(tmp_path.iterdir()) == []


def test_model_directory_symlink_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "models"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ModelDownloadError, match="symlink"):
        download_model_artifact(
            url="https://huggingface.co/org/repo/file.gguf",
            sha256="a" * 64,
            filename="model.gguf",
            model_directory=link,
        )

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target, target_is_directory=True)
    nested_directory = target / "nested"
    nested_directory.mkdir()
    with pytest.raises(ModelDownloadError, match="symlink"):
        download_model_artifact(
            url="https://huggingface.co/org/repo/file.gguf",
            sha256="a" * 64,
            filename="model.gguf",
            model_directory=parent_link / "nested",
        )


def test_redirect_count_and_artifact_limit_are_bounded(tmp_path):
    connections = []

    def make_redirect(*_args, **_kwargs):
        connections.append(True)
        return FakeConnection(
            FakeResponse(
                status=302,
                headers={"Location": "https://huggingface.co/org/repo/file.gguf"},
            )
        )

    with pytest.raises(ModelDownloadError, match="redirect limit"):
        download_model_artifact(
            url="https://huggingface.co/org/repo/file.gguf",
            sha256="a" * 64,
            filename="model.gguf",
            model_directory=tmp_path,
            resolver=lambda _host: ["8.8.8.8"],
            connection_factory=make_redirect,
            required_group_id=tmp_path.stat().st_gid,
        )

    assert len(connections) == 6
    assert MAX_ARTIFACT_BYTES == 100 * 1024**3
    assert list(tmp_path.iterdir()) == []
