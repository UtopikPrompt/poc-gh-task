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
from github_service.publishing import FileChangeProposal, GitHubTaskPublisher
from github_service.settings import RuntimeSettings
from github_service.source_context import GitHubTaskSource
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
    propose_file_changes_factory: Callable[
        [object], Callable[[FileChangeProposal], dict[str, object]]
    ]
    | None = None,
    source_context_factory: Callable[[object], GitHubTaskSource] | None = None,
) -> FastAPI:
    settings = settings or RuntimeSettings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = (
            PostgresDeliveryStore(settings.github_database_url)
            if settings.github_database_url
            else SQLiteDeliveryStore(settings.database_path)
        )
        source_context = (
            source_context_factory(store)
            if source_context_factory is not None
            else None
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
            propose_file_changes=(
                propose_file_changes_factory(store)
                if propose_file_changes_factory is not None
                else None
            ),
            get_source_tree=(
                source_context.candidate_paths if source_context is not None else None
            ),
            get_source_files=(
                source_context.read_files if source_context is not None else None
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
    client = GitHubAppClient.from_settings(settings)
    normalizer = GitHubEventNormalizer(
        client,
        ProjectEventConfig(
            status_field_name=settings.project_status_field_name,
            ready_option_name=settings.project_ready_option_name,
        ),
    )
    return create_app(
        normalize_event=normalizer,
        settings=settings,
        close_resources=(client.close,),
        complete_task_factory=lambda store: GitHubTaskCompletion(
            store=store,
            github=client,
        ),
        propose_file_changes_factory=lambda store: (
            GitHubTaskPublisher(
                store=store,
                github=client,
            ).publish
        ),
        source_context_factory=lambda store: GitHubTaskSource(
            store=store,
            github=client,
        ),
    )


def create_host_app() -> FastAPI:
    """Create the production GitHub service using PostgreSQL and App credentials."""
    settings = RuntimeSettings.from_environment()
    if not settings.github_database_url:
        raise ValueError("GITHUB_DB_* settings must be configured for production")
    if not settings.internal_service_token:
        raise ValueError("INTERNAL_SERVICE_TOKEN must be configured")
    return create_github_app(settings=settings)
