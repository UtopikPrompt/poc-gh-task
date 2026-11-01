"""Standalone LangGraph worker service entrypoint."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from langgraph_service.database import postgres_url_from_environment
from langgraph_service.github_client import (
    GitHubServiceClient,
    build_submit_proposal_operation,
)
from langgraph_service.local_model import LocalLlamaCppProposalProvider
from langgraph_service.operations import WorkflowOperations
from langgraph_service.service import LangGraphTaskService
from langgraph_service.worker import LangGraphEventWorker

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    database_url = postgres_url_from_environment("LANGGRAPH", required=True)
    github_service_url = os.environ.get("GITHUB_SERVICE_URL", "").strip()
    service_token = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
    module_name = (
        os.environ.get("WORKFLOW_OPERATIONS_MODULE", "").strip()
        or "langgraph_service.workflow_operations"
    )
    missing = [
        name
        for name, value in (
            ("LANGGRAPH_DB_*", database_url),
            ("GITHUB_SERVICE_URL", github_service_url),
            ("INTERNAL_SERVICE_TOKEN", service_token),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"LangGraph service settings are missing: {', '.join(missing)}"
        )
    poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "1"))
    if poll_seconds <= 0:
        raise ValueError("WORKER_POLL_SECONDS must be greater than zero")

    module = importlib.import_module(module_name)
    make_operations = getattr(module, "make_operations", None)
    if not callable(make_operations):
        raise TypeError(f"Worker module {module_name!r} must export make_operations()")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        github_client = GitHubServiceClient(
            service_url=github_service_url,
            token=service_token,
        )
        proposal_provider = None
        try:
            if module_name == "langgraph_service.workflow_operations":
                proposal_provider = LocalLlamaCppProposalProvider.from_environment()
                operations = make_operations(
                    github_client=github_client,
                    proposal_provider=proposal_provider,
                )
            else:
                operations = make_operations()
            if not isinstance(operations, WorkflowOperations):
                raise TypeError("make_operations() must return WorkflowOperations")
            with LangGraphTaskService(
                database_url=database_url,
                operations=operations,
                publish_pr=build_submit_proposal_operation(github_client),
            ) as lifecycle:
                worker = LangGraphEventWorker(
                    queue=github_client,
                    lifecycle=lifecycle,
                    readiness=(
                        proposal_provider.is_ready
                        if proposal_provider is not None
                        else None
                    ),
                )
                app.state.proposal_provider = proposal_provider

                async def consume_events() -> None:
                    while True:
                        try:
                            processed = await run_in_threadpool(worker.process_one)
                        except Exception:
                            logger.exception("LangGraph event processing failed")
                            processed = True
                        if not processed:
                            await asyncio.sleep(poll_seconds)

                task = asyncio.create_task(consume_events())
                app.state.event_worker = worker
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        finally:
            app.state.proposal_provider = None
            if proposal_provider is not None:
                proposal_provider.close()
            github_client.close()

    app = FastAPI(lifespan=lifespan)
    app.state.local_model_required = (
        module_name == "langgraph_service.workflow_operations"
    )
    app.state.proposal_provider = None

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def readyz() -> dict[str, str] | JSONResponse:
        if not app.state.local_model_required:
            return {"status": "ok"}
        provider = app.state.proposal_provider
        if provider is None or not await run_in_threadpool(provider.is_ready):
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "dependency": "local_model"},
            )
        return {"status": "ok"}

    return app
