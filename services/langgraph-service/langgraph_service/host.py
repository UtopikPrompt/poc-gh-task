"""Standalone LangGraph worker service entrypoint."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from langgraph_service.github_client import (
    GitHubServiceClient,
    build_publish_pr_operation,
)
from langgraph_service.operations import WorkflowOperations
from langgraph_service.service import LangGraphTaskService
from langgraph_service.worker import LangGraphEventWorker

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    database_url = os.environ.get("LANGGRAPH_DATABASE_URL", "").strip()
    github_service_url = os.environ.get("GITHUB_SERVICE_URL", "").strip()
    service_token = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
    module_name = os.environ.get("WORKFLOW_OPERATIONS_MODULE", "").strip()
    missing = [
        name
        for name, value in (
            ("LANGGRAPH_DATABASE_URL", database_url),
            ("GITHUB_SERVICE_URL", github_service_url),
            ("INTERNAL_SERVICE_TOKEN", service_token),
            ("WORKFLOW_OPERATIONS_MODULE", module_name),
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
    operations = make_operations()
    if not isinstance(operations, WorkflowOperations):
        raise TypeError("make_operations() must return WorkflowOperations")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        github_client = GitHubServiceClient(
            service_url=github_service_url,
            token=service_token,
        )
        with LangGraphTaskService(
            database_url=database_url,
            operations=operations,
            publish_pr=build_publish_pr_operation(github_client),
        ) as lifecycle:
            worker = LangGraphEventWorker(queue=github_client, lifecycle=lifecycle)

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
                github_client.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
