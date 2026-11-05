"""Validated, bounded downloads of public Hugging Face model artifacts."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import time
import ssl
from typing import Callable
from urllib.parse import parse_qsl, urljoin, urlsplit

MAX_ARTIFACT_BYTES = 100 * 1024**3
MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT_SECONDS = 30 * 60
_MODEL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf\Z")
_SHA256 = re.compile(r"[A-Fa-f0-9]{64}\Z")
_REDIRECT_HOST_SUFFIXES = (".huggingface.co", ".hf.co")
_CREDENTIAL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "credential",
    "key",
    "private_token",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-signature",
}


class ModelDownloadError(Exception):
    """A failure safe to classify without exposing upstream details."""


def validate_model_filename(filename: str) -> str:
    if not isinstance(filename, str) or not _MODEL_FILENAME.fullmatch(filename):
        raise ValueError("model filename is invalid")
    return filename


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("SHA-256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def validate_download_url(value: str) -> str:
    """Accept only credential-free HTTPS URLs on the canonical HF host."""
    if not isinstance(value, str) or not value or any(ord(char) < 33 for char in value):
        raise ValueError("model URL must be a valid HTTPS Hugging Face URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("model URL must be a valid HTTPS Hugging Face URL") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "huggingface.co"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        or not parsed.path.startswith("/")
        or any(
            key.casefold() in _CREDENTIAL_QUERY_KEYS
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise ValueError(
            "model URL must use HTTPS on huggingface.co without credentials"
        )
    return value


def _validate_redirect_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ModelDownloadError("redirect rejected") from error
    host = parsed.hostname or ""
    is_huggingface_host = host == "huggingface.co" or any(
        host.endswith(suffix) and len(host) > len(suffix)
        for suffix in _REDIRECT_HOST_SUFFIXES
    )
    if (
        parsed.scheme != "https"
        or not is_huggingface_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
    ):
        raise ModelDownloadError("redirect rejected")
    return value


def _public_addresses(host: str) -> list[str]:
    try:
        results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(result[4][0] for result in results))
    except OSError as error:
        raise ModelDownloadError("model host could not be resolved") from error
    if not addresses:
        raise ModelDownloadError("model host could not be resolved")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as error:
            raise ModelDownloadError(
                "model host resolved to an invalid address"
            ) from error
        if (
            not parsed.is_global
            or parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_unspecified
            or parsed.is_reserved
        ):
            raise ModelDownloadError("model host resolved to a prohibited address")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, *, timeout: float):
        super().__init__(
            host, 443, timeout=timeout, context=ssl.create_default_context()
        )
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def _request_target(url: str) -> str:
    parsed = urlsplit(url)
    return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")


def _open_directory_without_symlinks(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd: int | None = None
    try:
        directory_fd = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise ModelDownloadError("model directory is not a real directory")
        return directory_fd
    except ModelDownloadError:
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    except OSError as error:
        if directory_fd is not None:
            os.close(directory_fd)
        raise ModelDownloadError(
            "model directory is unavailable or contains a symlink"
        ) from error


def download_model_artifact(
    *,
    url: str,
    sha256: str,
    filename: str,
    model_directory: str | Path,
    on_verifying: Callable[[], None] | None = None,
    on_progress: Callable[[], None] | None = None,
    resolver: Callable[[str], list[str]] = _public_addresses,
    connection_factory: Callable[..., http.client.HTTPSConnection] = (
        _PinnedHTTPSConnection
    ),
    max_bytes: int = MAX_ARTIFACT_BYTES,
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    required_group_id: int = 20000,
) -> Path:
    """Stream, hash, and atomically publish one artifact without following DNS aliases."""
    current_url = validate_download_url(url)
    expected_hash = validate_sha256(sha256)
    safe_filename = validate_model_filename(filename)
    directory_path = Path(model_directory)
    if ".." in directory_path.parts:
        raise ModelDownloadError("model directory path is not safe")
    absolute_directory = directory_path.absolute()
    if max_bytes <= 0 or timeout_seconds <= 0:
        raise ValueError("download limits must be positive")

    deadline = time.monotonic() + timeout_seconds
    directory_fd: int | None = None
    temporary_name: str | None = None
    connection: http.client.HTTPSConnection | None = None
    try:
        directory_fd = _open_directory_without_symlinks(absolute_directory)
        file_descriptor = -1
        for _attempt in range(5):
            temporary_name = f".model-download-{secrets.token_hex(16)}.tmp"
            try:
                file_descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                continue
        if file_descriptor < 0:
            raise ModelDownloadError("temporary model file could not be created")
        digest = hashlib.sha256()
        bytes_written = 0
        redirects = 0
        with os.fdopen(file_descriptor, "wb") as artifact:
            if os.fstat(artifact.fileno()).st_gid != required_group_id:
                raise ModelDownloadError(
                    "model directory downloader group is incorrect"
                )
            while True:
                if time.monotonic() >= deadline:
                    raise ModelDownloadError("model download timed out")
                parsed = urlsplit(current_url)
                host = parsed.hostname or ""
                addresses = resolver(host)
                connection = connection_factory(
                    host,
                    addresses[0],
                    timeout=min(20, max(1, deadline - time.monotonic())),
                )
                connection.request(
                    "GET",
                    _request_target(current_url),
                    headers={
                        "Accept-Encoding": "identity",
                        "User-Agent": "CodefactoryModelDownloader/1.0",
                    },
                )
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    connection.close()
                    connection = None
                    redirects += 1
                    if not location or redirects > MAX_REDIRECTS:
                        raise ModelDownloadError("model redirect limit exceeded")
                    current_url = _validate_redirect_url(urljoin(current_url, location))
                    continue
                if response.status != 200:
                    raise ModelDownloadError(
                        "model host returned an unsuccessful response"
                    )
                content_length = response.getheader("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > max_bytes:
                            raise ModelDownloadError(
                                "model artifact exceeds the size limit"
                            )
                    except ValueError as error:
                        raise ModelDownloadError(
                            "model response size is invalid"
                        ) from error
                while True:
                    if time.monotonic() >= deadline:
                        raise ModelDownloadError("model download timed out")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise ModelDownloadError(
                            "model artifact exceeds the size limit"
                        )
                    digest.update(chunk)
                    artifact.write(chunk)
                    if on_progress is not None:
                        on_progress()
                connection.close()
                connection = None
                break

            if bytes_written == 0:
                raise ModelDownloadError("model artifact is empty")
            artifact.flush()
            os.fsync(artifact.fileno())
            if on_verifying is not None:
                on_verifying()
            if digest.hexdigest() != expected_hash:
                raise ModelDownloadError("model artifact SHA-256 did not match")
            if os.fstat(artifact.fileno()).st_gid != required_group_id:
                raise ModelDownloadError("model artifact group ownership changed")
            os.fchmod(artifact.fileno(), 0o640)
            os.fsync(artifact.fileno())

        os.replace(
            temporary_name,
            safe_filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        os.fsync(directory_fd)
        return absolute_directory / safe_filename
    except ModelDownloadError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as error:
        raise ModelDownloadError("model download failed") from error
    finally:
        if connection is not None:
            connection.close()
        if temporary_name is not None and directory_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
