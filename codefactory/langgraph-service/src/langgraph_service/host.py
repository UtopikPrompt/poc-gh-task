"""Standalone LangGraph worker service entrypoint."""

from __future__ import annotations

import asyncio
import hmac
import httpx
import importlib
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from langgraph_service.agent_config import AgentConfig
from langgraph_service.agent_roster import AgentRoster
from langgraph_service.inference_config import (
    InferenceInstance,
    InferenceInstanceConfig,
)
from langgraph_service.inference_metrics import PrometheusInferenceMetrics
from langgraph_service.model_download import (
    ModelDownloadError,
    resolve_huggingface_model_file,
    validate_download_url,
    validate_model_filename,
    validate_sha256,
)
from langgraph_service.model_artifact_api import AvailableModelFilenamesResponse
from langgraph_service.storage.agent_roster_store import (
    AgentRosterSnapshot,
    PostgresAgentRosterStore,
)
from langgraph_service.storage.config_store import PostgresAgentConfigStore
from langgraph_service.storage.inference_config_store import (
    PostgresInferenceInstanceConfigStore,
)
from langgraph_service.storage.model_download_store import (
    ModelDownloadRequest,
    PUBLIC_FAILURES,
    PostgresModelDownloadStore,
    ROLLOUT_FAILURE,
)
from langgraph_service.storage.database import postgres_url_from_environment
from langgraph_service.integrations.github_client import (
    GitHubServiceClient,
    build_submit_proposal_operation,
)
from langgraph_service.providers.local_model import LocalLlamaCppProposalProvider
from langgraph_service.workflow.operations import WorkflowOperations
from langgraph_service.workflow.service import LangGraphTaskService
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


class InferenceInstanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instance_id: InferenceInstance
    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    updated_by: str = Field(
        strict=True,
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    )


class InferenceInstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instance_id: InferenceInstance
    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    created_by: str = Field(
        strict=True,
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    )


class InferenceInstanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: InferenceInstanceConfig
    version: int
    updated_at: datetime | None
    updated_by: str | None


class ModelDownloadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    model_url: str = Field(max_length=4096)
    requested_by: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    )


class ModelDownloadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    model_filename: str
    requested_by: str
    status: Literal["queued", "downloading", "verifying", "ready", "failed"]
    public_error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class ModelRolloutClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lease_seconds: int = Field(default=120, ge=30, le=900)


class ModelRolloutEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    instance_id: InferenceInstance
    rollout_request_id: UUID
    model_filename: str
    config_version: int
    idempotency_key: str
    lease_token: UUID
    lease_until: datetime
    attempts: int


class ModelRolloutClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: ModelRolloutEvent | None


class ModelRolloutAcknowledgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: UUID
    outcome: Literal["succeeded", "failed"]


class ModelRolloutAcknowledgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    status: Literal["succeeded", "failed", "superseded"]


class MetricValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None
    status: Literal["available", "stale", "unavailable"]


class InferenceInstanceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: InferenceInstance
    cpu_utilization_percent: MetricValue
    ram_used_bytes: MetricValue
    gpu_utilization_percent: MetricValue
    vram_used_bytes: MetricValue
    vram_total_bytes: MetricValue
    sampled_at: datetime | None


class InferenceMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instances: list[InferenceInstanceMetrics]


def _inference_instance_response(snapshot) -> dict[str, object]:
    config = snapshot.config.model_dump()
    if (
        config["rollout_error"] is not None
        and config["rollout_error"] != ROLLOUT_FAILURE
    ):
        config["rollout_error"] = ROLLOUT_FAILURE
    return {
        "config": config,
        "version": snapshot.version,
        "updated_at": (
            snapshot.updated_at.isoformat() if snapshot.updated_at is not None else None
        ),
        "updated_by": snapshot.updated_by,
    }


def _model_download_response(request: ModelDownloadRequest) -> dict[str, object]:
    public_error = request.public_error
    if public_error is not None and public_error not in PUBLIC_FAILURES.values():
        public_error = PUBLIC_FAILURES["download_failed"]
    return {
        "request_id": str(request.request_id),
        "model_filename": request.model_filename,
        "requested_by": request.requested_by,
        "status": request.status,
        "public_error": public_error,
        "attempts": request.attempts,
        "created_at": request.created_at.isoformat(),
        "updated_at": request.updated_at.isoformat(),
    }


class AgentRosterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    roster: AgentRoster
    expected_revision: int = Field(ge=0)
    updated_by: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    )


class AgentRosterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roster: AgentRoster | None
    revision: int
    updated_at: str | None
    updated_by: str | None


class NoCIResolutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    actor: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$",
    )
    decision: Literal["proceed_without_ci"]


def _snapshot_response(snapshot) -> dict[str, object]:
    return {
        "config": snapshot.config.model_dump(),
        "version": snapshot.version,
        "updated_at": (
            snapshot.updated_at.isoformat() if snapshot.updated_at is not None else None
        ),
        "updated_by": snapshot.updated_by,
    }


def _roster_response(snapshot: AgentRosterSnapshot) -> dict[str, object]:
    return {
        "roster": (
            snapshot.roster.model_dump(mode="json")
            if snapshot.roster is not None
            else None
        ),
        "revision": snapshot.revision,
        "updated_at": (
            snapshot.updated_at.isoformat() if snapshot.updated_at is not None else None
        ),
        "updated_by": snapshot.updated_by,
    }


def create_app() -> FastAPI:
    database_url = postgres_url_from_environment("LANGGRAPH", required=True)
    github_service_url = os.environ.get("GITHUB_SERVICE_URL", "").strip()
    model_artifact_api_url = os.environ.get("MODEL_ARTIFACT_API_URL", "").strip()
    service_token = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
    reconciler_token = os.environ.get("MODEL_RECONCILER_TOKEN", "")
    module_name = (
        os.environ.get("WORKFLOW_OPERATIONS_MODULE", "").strip()
        or "langgraph_service.workflow.workflow_operations"
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
    inference_config_store = PostgresInferenceInstanceConfigStore(database_url)
    model_download_store = PostgresModelDownloadStore(database_url)
    inference_metrics = PrometheusInferenceMetrics(
        os.environ.get("PROMETHEUS_URL", "").strip() or None
    )
    roster_store = PostgresAgentRosterStore(database_url)
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
            if module_name == "langgraph_service.workflow.workflow_operations":
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
                agent_roster_store=roster_store,
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

    @app.exception_handler(RequestValidationError)
    async def redact_download_validation_error(request, _error):
        if request.url.path == "/internal/management/inference-model-downloads":
            return JSONResponse(
                status_code=422,
                content={"detail": "model download request was rejected"},
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, _error)

    app.state.local_model_required = (
        module_name == "langgraph_service.workflow.workflow_operations"
    )
    app.state.proposal_provider = None

    def authenticate_internal(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {service_token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    def authenticate_model_reconciler(
        authorization: str = Header(default=""),
    ) -> None:
        if not reconciler_token:
            raise HTTPException(
                status_code=503, detail="model reconciler authentication unavailable"
            )
        if not hmac.compare_digest(authorization, f"Bearer {reconciler_token}"):
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
            completion_timeout_seconds=payload.completion_timeout_seconds,
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
        "/internal/management/inference-instances",
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_inference_instances():
        try:
            snapshots = await run_in_threadpool(inference_config_store.get_all)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error
        return {
            "instances": [
                _inference_instance_response(snapshots[instance_id])
                for instance_id in snapshots
            ]
        }

    @app.post(
        "/internal/management/inference-instances",
        response_model=InferenceInstanceRecord,
        status_code=201,
        dependencies=[Depends(authenticate_internal)],
    )
    async def create_inference_instance(payload: InferenceInstanceCreate):
        config = InferenceInstanceConfig(
            instance_id=payload.instance_id,
            model_filename=payload.model_filename,
        )
        try:
            snapshot = await run_in_threadpool(
                inference_config_store.create,
                config,
                created_by=payload.created_by,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=409, detail="inference instance already exists"
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error
        return _inference_instance_response(snapshot)

    @app.get(
        "/internal/management/inference-models",
        response_model=AvailableModelFilenamesResponse,
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_available_inference_models(request: Request):
        if not model_artifact_api_url or not reconciler_token:
            raise HTTPException(
                status_code=503, detail="available model listing unavailable"
            )
        transport = getattr(request.app.state, "model_artifact_http_transport", None)
        try:
            async with httpx.AsyncClient(
                base_url=model_artifact_api_url.rstrip("/"),
                transport=transport,
                timeout=10.0,
            ) as client:
                response = await client.get(
                    "/internal/models/available",
                    headers={"Authorization": f"Bearer {reconciler_token}"},
                )
            response.raise_for_status()
            record = AvailableModelFilenamesResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise HTTPException(
                status_code=503, detail="available model listing unavailable"
            ) from error
        return record.model_dump(mode="json")

    @app.put(
        "/internal/management/inference-instances/{instance_id}",
        response_model=InferenceInstanceRecord,
        dependencies=[Depends(authenticate_internal)],
    )
    async def update_inference_instance(
        instance_id: InferenceInstance, payload: InferenceInstanceUpdate
    ):
        if payload.instance_id != instance_id:
            raise HTTPException(status_code=422, detail="instance does not match path")
        config = InferenceInstanceConfig(
            instance_id=payload.instance_id,
            model_filename=payload.model_filename,
        )
        try:
            snapshot = await run_in_threadpool(
                inference_config_store.update,
                config,
                updated_by=payload.updated_by,
            )
        except ValueError as error:
            active = str(error) == "inference model download is active"
            rollout_active = str(error) == "inference model rollout is active"
            raise HTTPException(
                status_code=409 if active or rollout_active else 404,
                detail=(
                    "inference model download is active"
                    if active
                    else "inference model rollout is active"
                    if rollout_active
                    else "inference instance not found"
                ),
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error
        return _inference_instance_response(snapshot)

    @app.delete(
        "/internal/management/inference-instances/{instance_id}",
        dependencies=[Depends(authenticate_internal)],
    )
    async def delete_inference_instance(instance_id: InferenceInstance):
        try:
            await run_in_threadpool(inference_config_store.delete, instance_id)
        except ValueError as error:
            detail = str(error)
            if detail == "unknown inference instance":
                raise HTTPException(status_code=404, detail=detail) from error
            if detail in {
                "inference model download is active",
                "inference model rollout is active",
                "at least one inference instance must remain",
            }:
                raise HTTPException(status_code=409, detail=detail) from error
            raise HTTPException(
                status_code=422, detail="inference instance deletion was rejected"
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error
        return {"deleted": True, "instance_id": instance_id}

    @app.post(
        "/internal/management/inference-model-downloads",
        response_model=ModelDownloadRecord,
        status_code=202,
        dependencies=[Depends(authenticate_internal)],
    )
    async def request_model_download(payload: ModelDownloadPayload):
        try:
            if len(payload.model_url) > 4096:
                raise ValueError("model URL is too long")
            validate_model_filename(payload.model_filename)
            download_url, sha256 = await run_in_threadpool(
                resolve_huggingface_model_file, payload.model_url
            )
            validate_download_url(download_url)
            validate_sha256(sha256)
            request = await run_in_threadpool(
                model_download_store.submit,
                model_filename=payload.model_filename,
                model_url=download_url,
                model_sha256=sha256,
                requested_by=payload.requested_by,
            )
        except ModelDownloadError as error:
            raise HTTPException(
                status_code=422,
                detail="Hugging Face model file could not be verified",
            ) from error
        except ValueError as error:
            active = str(error) == "a shared model download is already active"
            raise HTTPException(
                status_code=409 if active else 422,
                detail=(
                    "a shared model download is already active"
                    if active
                    else "model download request was rejected"
                ),
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="model download queue unavailable"
            ) from error
        return _model_download_response(request)

    @app.get(
        "/internal/management/inference-model-downloads/{request_id}",
        response_model=ModelDownloadRecord,
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_model_download_status(request_id: UUID):
        try:
            request = await run_in_threadpool(model_download_store.get, request_id)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="model download status unavailable"
            ) from error
        if request is None:
            raise HTTPException(status_code=404, detail="model download not found")
        return _model_download_response(request)

    @app.post(
        "/internal/reconciler/inference-model-rollouts/claim",
        response_model=ModelRolloutClaimResponse,
        dependencies=[Depends(authenticate_model_reconciler)],
    )
    async def claim_model_rollout(payload: ModelRolloutClaimPayload):
        try:
            event = await run_in_threadpool(
                model_download_store.claim_rollout,
                lease_seconds=payload.lease_seconds,
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="model rollout queue unavailable"
            ) from error
        return {"event": event}

    @app.post(
        "/internal/reconciler/inference-model-rollouts/{event_id}/ack",
        response_model=ModelRolloutAcknowledgeResponse,
        dependencies=[Depends(authenticate_model_reconciler)],
    )
    async def acknowledge_model_rollout(
        event_id: UUID, payload: ModelRolloutAcknowledgePayload
    ):
        try:
            status = await run_in_threadpool(
                model_download_store.acknowledge_rollout,
                event_id,
                payload.lease_token,
                outcome=payload.outcome,
            )
        except ValueError as error:
            detail = str(error)
            if detail == "rollout event not found":
                raise HTTPException(
                    status_code=404, detail="rollout event not found"
                ) from error
            raise HTTPException(
                status_code=409, detail="rollout acknowledgment is no longer active"
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="model rollout status unavailable"
            ) from error
        return {"event_id": event_id, "status": status}

    @app.get(
        "/internal/management/inference-metrics",
        response_model=InferenceMetricsResponse,
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_inference_metrics():
        try:
            snapshots = await run_in_threadpool(inference_config_store.get_all)
            return await run_in_threadpool(
                inference_metrics.get_metrics, tuple(snapshots)
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference metrics unavailable"
            ) from error

    @app.get(
        "/internal/management/agent-roster",
        response_model=AgentRosterResponse,
        dependencies=[Depends(authenticate_internal)],
    )
    async def get_agent_roster():
        try:
            snapshot = await run_in_threadpool(roster_store.get)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="agent roster unavailable"
            ) from error
        return _roster_response(snapshot)

    @app.put(
        "/internal/management/agent-roster",
        response_model=AgentRosterResponse,
        dependencies=[Depends(authenticate_internal)],
    )
    async def update_agent_roster(payload: AgentRosterUpdate):
        try:
            snapshot = await run_in_threadpool(
                roster_store.update,
                payload.roster,
                expected_revision=payload.expected_revision,
                updated_by=payload.updated_by,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="agent roster unavailable"
            ) from error
        return _roster_response(snapshot)

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

    @app.post(
        "/internal/management/executions/{thread_id}/resolve-no-ci",
        dependencies=[Depends(authenticate_internal)],
    )
    async def resolve_execution_without_ci(
        thread_id: str,
        payload: NoCIResolutionPayload,
    ) -> dict[str, object]:
        if not thread_id or len(thread_id) > 200:
            raise HTTPException(status_code=404, detail="execution not found")
        lifecycle = get_lifecycle()
        try:
            result = await run_in_threadpool(
                lifecycle.invoke,
                {
                    "task_id": thread_id,
                    "event_type": "resolve_no_ci",
                    "operator_actor": payload.actor,
                    "operator_decision": payload.decision,
                    "operator_decided_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="execution state unavailable"
            ) from error
        if result.get("event_status") != "no_ci_resolved":
            raise HTTPException(
                status_code=409, detail="execution has no unresolved CI gap"
            )
        return {"resolved": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def readyz() -> dict[str, str] | JSONResponse:
        if not app.state.local_model_required:
            return {"status": "ok"}
        provider = app.state.proposal_provider
        try:
            ready = provider is not None and await run_in_threadpool(provider.is_ready)
        except Exception:
            ready = False
        if not ready:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "dependency": "local_model"},
            )
        return {"status": "ok"}

    return app
