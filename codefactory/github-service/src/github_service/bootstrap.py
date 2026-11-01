"""Composition root for the configured GitHub webhook application."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from github_service.completion import GitHubTaskCompletion
from github_service.delivery_store import (
    PostgresDeliveryStore,
    SQLiteDeliveryStore,
)
from github_service.events import (
    GitHubEventNormalizer,
    ProjectEventConfig,
)
from github_service.fastapi import (
    InternalServiceRuntime,
    WebhookRuntime,
    create_http_app,
)
from github_service.github_app import GitHubAppClient
from github_service.publishing import GitHubTaskPublisher
from github_service.settings import RuntimeSettings
from github_service.webhook_service import (
    EventNormalizer,
    WebhookService,
)


def create_app(
    *,
    normalize_event: EventNormalizer,
    settings: RuntimeSettings | None = None,
    close_resources: Sequence[Callable[[], None]] = (),
    complete_task_factory: Callable[[object], Callable[[str], dict[str, object]]]
    | None = None,
    publish_pull_request_factory: Callable[[object], Callable[..., dict[str, object]]]
    | None = None,
) -> FastAPI:
    settings = settings or RuntimeSettings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = (
            PostgresDeliveryStore(settings.github_database_url)
            if settings.github_database_url
            else SQLiteDeliveryStore(settings.database_path)
        )
        service = WebhookService(store=store, normalize_event=normalize_event)
        app.state.webhook_runtime = WebhookRuntime(
            service=service,
            webhook_secret=settings.github_webhook_secret,
        )
        app.state.internal_runtime = InternalServiceRuntime(
            store=store,
            token=settings.internal_service_token,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
            complete_task=(
                complete_task_factory(store)
                if complete_task_factory is not None
                else None
            ),
            publish_pull_request=(
                publish_pull_request_factory(store)
                if publish_pull_request_factory is not None
                else None
            ),
        )
        try:
            yield
        finally:
            store.close()
            for close_resource in reversed(close_resources):
                close_resource()

    return create_http_app(settings=settings, lifespan=lifespan)


def create_github_app(
    *,
    settings: RuntimeSettings | None = None,
) -> FastAPI:
    """Create the standalone GitHub webhook and capability service."""
    settings = settings or RuntimeSettings.from_environment()
    if not settings.project_ready_option_id:
        raise ValueError("GITHUB_PROJECT_READY_OPTION_ID must be configured")

    client = GitHubAppClient.from_settings(settings)
    normalizer = GitHubEventNormalizer(
        client,
        ProjectEventConfig(
            project_node_id=settings.project_node_id,
            status_field_id=settings.project_status_field_id,
            ready_option_id=settings.project_ready_option_id,
        ),
    )
    return create_app(
        normalize_event=normalizer,
        settings=settings,
        close_resources=(client.close,),
        complete_task_factory=lambda store: GitHubTaskCompletion(
            store=store,
            github=client,
            project_node_id=settings.project_node_id,
        ),
        publish_pull_request_factory=lambda store: (
            GitHubTaskPublisher(
                store=store,
                github=client,
            ).publish
        ),
    )


def create_host_app() -> FastAPI:
    """Create the production GitHub service using PostgreSQL and App credentials."""
    settings = RuntimeSettings.from_environment()
    if not settings.github_database_url:
        raise ValueError("GITHUB_DATABASE_URL must be configured for production")
    if not settings.internal_service_token:
        raise ValueError("INTERNAL_SERVICE_TOKEN must be configured")
    return create_github_app(settings=settings)
