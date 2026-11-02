"""Environment-backed settings for the webhook service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from github_service.database import postgres_url_from_environment


@dataclass(frozen=True)
class RuntimeSettings:
    github_webhook_secret: str
    database_path: Path
    github_database_url: str | None = field(default=None, repr=False)
    internal_service_token: str = ""
    max_webhook_bytes: int = 1_000_000
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 60
    worker_max_attempts: int = 8
    github_app_id: int | None = None
    github_private_key_path: Path | None = None
    github_api_url: str = "https://api.github.com"
    project_status_field_name: str = "Status"
    project_ready_option_name: str = "Ready"
    project_done_option_name: str = "Done"

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        database_path = Path(
            os.environ.get("LOCAL_SQLITE_PATH", "./var/github-local.sqlite")
        )
        if not secret:
            raise ValueError("GITHUB_WEBHOOK_SECRET must be configured")
        max_webhook_bytes = int(os.environ.get("WEBHOOK_MAX_BYTES", "1000000"))
        worker_poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "1"))
        worker_lease_seconds = int(os.environ.get("WORKER_LEASE_SECONDS", "60"))
        worker_max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "8"))
        if not 1 <= max_webhook_bytes <= 10_000_000:
            raise ValueError("WEBHOOK_MAX_BYTES must be between 1 and 10000000")
        if worker_poll_seconds <= 0 or worker_lease_seconds <= 0:
            raise ValueError("worker poll and lease settings must be positive")
        if worker_max_attempts <= 0:
            raise ValueError("WORKER_MAX_ATTEMPTS must be positive")
        return cls(
            github_webhook_secret=secret,
            database_path=database_path,
            github_database_url=postgres_url_from_environment("GITHUB", required=False),
            internal_service_token=os.environ.get("INTERNAL_SERVICE_TOKEN", ""),
            max_webhook_bytes=max_webhook_bytes,
            worker_poll_seconds=worker_poll_seconds,
            worker_lease_seconds=worker_lease_seconds,
            worker_max_attempts=worker_max_attempts,
            github_app_id=_optional_int("GITHUB_APP_ID"),
            github_private_key_path=_optional_path("GITHUB_PRIVATE_KEY_PATH"),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            project_status_field_name=os.environ.get(
                "GITHUB_PROJECT_STATUS_FIELD_NAME", "Status"
            ),
            project_ready_option_name=os.environ.get(
                "GITHUB_PROJECT_READY_OPTION_NAME", "Ready"
            ),
            project_done_option_name=os.environ.get(
                "GITHUB_PROJECT_DONE_OPTION_NAME", "Done"
            ),
        )


def _optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    return None if not value else int(value)


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return None if not value else Path(value)
