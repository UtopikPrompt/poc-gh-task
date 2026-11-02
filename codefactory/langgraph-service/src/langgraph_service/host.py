"""Standalone LangGraph worker service entrypoint."""

from __future__ import annotations

import asyncio
import hmac
import importlib
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from langgraph_service.agent_config import AgentConfig
from langgraph_service.config_store import PostgresAgentConfigStore
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


class AgentConfigUpdate(AgentConfig):
    updated_by: str = Field(
        strict=True,
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    )


class AgentConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: AgentConfig
    version: int
    updated_at: str | None
    updated_by: str | None


def _snapshot_response(snapshot) -> dict[str, object]:
    return {
        "config": snapshot.config.model_dump(),
        "version": snapshot.version,
        "updated_at": (
            snapshot.updated_at.isoformat() if snapshot.updated_at is not None else None
        ),
        "updated_by": snapshot.updated_by,
    }


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
    config_store = PostgresAgentConfigStore(database_url)
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
                agent_config_store=config_store,
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
                app.state.task_lifecycle = lifecycle
                try:
                    yield
                finally:
                    app.state.task_lifecycle = None
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

    def authenticate_internal(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {service_token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    def get_lifecycle() -> LangGraphTaskService:
        lifecycle = getattr(app.state, "task_lifecycle", None)
        if not isinstance(lifecycle, LangGraphTaskService):
            raise HTTPException(status_code=503, detail="workflow service unavailable")
        return lifecycle

    @app.get(
        "/internal/management/agent-config",
        response_model=AgentConfigResponse,
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_agent_config():
        try:
            snapshot = await run_in_threadpool(config_store.get)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="agent configuration unavailable"
            ) from error
        return _snapshot_response(snapshot)

    @app.put(
        "/internal/management/agent-config",
        response_model=AgentConfigResponse,
        dependencies=[Depends(authenticate_internal)],
    )
    async def update_agent_config(payload: AgentConfigUpdate):
        config = AgentConfig(
            completion_timeout_seconds=payload.completion_timeout_seconds
        )
        try:
            snapshot = await run_in_threadpool(
                config_store.update, config, updated_by=payload.updated_by
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="agent configuration unavailable"
            ) from error
        return _snapshot_response(snapshot)

    @app.get(
        "/internal/management/executions",
        dependencies=[Depends(authenticate_internal)],
    )
    async def list_executions(limit: int = Query(default=100, ge=1, le=100)):
        lifecycle = get_lifecycle()
        try:
            result = await run_in_threadpool(lifecycle.list_executions, limit=limit)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="execution state unavailable"
            ) from error
        return {"executions": result}

    @app.get(
        "/internal/management/executions/{thread_id}",
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_execution(thread_id: str):
        if not thread_id or len(thread_id) > 200:
            raise HTTPException(status_code=404, detail="execution not found")
        lifecycle = get_lifecycle()
        try:
            execution = await run_in_threadpool(lifecycle.get_execution, thread_id)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="execution state unavailable"
            ) from error
        if execution is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return execution

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
