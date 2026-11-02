"""Environment-backed settings for the management portal."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


GITHUB_OAUTH_CALLBACK_URL = "https://codefactory.utopiklab.com/auth"


@dataclass(frozen=True)
class PortalSettings:
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = field(default=None, repr=False)
    dashboard_session_signing_key: str | None = field(default=None, repr=False)
    github_oauth_callback_url: str = GITHUB_OAUTH_CALLBACK_URL
    github_api_url: str = "https://api.github.com"
    github_service_url: str = "http://github-service:8000"
    langgraph_service_url: str = "http://langgraph-service:8001"
    internal_service_token: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls) -> PortalSettings:
        return cls(
            github_oauth_client_id=os.environ.get("GITHUB_OAUTH_CLIENT_ID"),
            github_oauth_client_secret=os.environ.get("GITHUB_OAUTH_CLIENT_SECRET"),
            dashboard_session_signing_key=os.environ.get(
                "DASHBOARD_SESSION_SIGNING_KEY"
            ),
            github_oauth_callback_url=os.environ.get(
                "GITHUB_OAUTH_CALLBACK_URL", GITHUB_OAUTH_CALLBACK_URL
            ),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            github_service_url=os.environ.get(
                "GITHUB_SERVICE_URL", "http://github-service:8000"
            ),
            langgraph_service_url=os.environ.get(
                "LANGGRAPH_SERVICE_URL", "http://langgraph-service:8001"
            ),
            internal_service_token=os.environ.get("INTERNAL_SERVICE_TOKEN", ""),
        )
