"""Environment-backed settings for the webhook service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeSettings:
    github_webhook_secret: str
    database_path: Path
    github_database_url: str | None = None
    langgraph_database_url: str | None = None
    github_service_url: str = "http://github-service:8000"
    internal_service_token: str = ""
    max_webhook_bytes: int = 1_000_000
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 60
    worker_max_attempts: int = 8
    github_app_id: int | None = None
    github_installation_id: int | None = None
    github_private_key_path: Path | None = None
    github_api_url: str = "https://api.github.com"
    project_node_id: str = ""
    project_status_field_id: str = ""
    project_ready_option_id: str = ""
    project_done_option_id: str = ""

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
            github_database_url=os.environ.get("GITHUB_DATABASE_URL"),
            langgraph_database_url=os.environ.get("LANGGRAPH_DATABASE_URL"),
            github_service_url=os.environ.get(
                "GITHUB_SERVICE_URL", "http://github-service:8000"
            ),
            internal_service_token=os.environ.get("INTERNAL_SERVICE_TOKEN", ""),
            max_webhook_bytes=max_webhook_bytes,
            worker_poll_seconds=worker_poll_seconds,
            worker_lease_seconds=worker_lease_seconds,
            worker_max_attempts=worker_max_attempts,
            github_app_id=_optional_int("GITHUB_APP_ID"),
            github_installation_id=_optional_int("GITHUB_INSTALLATION_ID"),
            github_private_key_path=_optional_path("GITHUB_PRIVATE_KEY_PATH"),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            project_node_id=os.environ.get("GITHUB_PROJECT_NODE_ID", ""),
            project_status_field_id=os.environ.get(
                "GITHUB_PROJECT_STATUS_FIELD_ID", ""
            ),
            project_ready_option_id=os.environ.get(
                "GITHUB_PROJECT_READY_OPTION_ID", ""
            ),
            project_done_option_id=os.environ.get("GITHUB_PROJECT_DONE_OPTION_ID", ""),
        )


def _optional_int(name: str) -> int | None:
    value = os.environ.get(name)
    return None if not value else int(value)


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return None if not value else Path(value)
