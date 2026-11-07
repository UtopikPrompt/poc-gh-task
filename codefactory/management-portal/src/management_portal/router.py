"""HTTP routes and packaged UI for the management portal."""

from __future__ import annotations

import hmac
import httpx
from pathlib import Path
import re
import secrets
from typing import Annotated, Literal
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from fastapi.staticfiles import StaticFiles

from management_portal.auth import (
    DASHBOARD_SESSION_COOKIE,
    DASHBOARD_SESSION_SECONDS,
    DASHBOARD_STATE_COOKIE,
    DASHBOARD_STATE_SECONDS,
    GITHUB_OAUTH_AUTHORIZE_URL,
    OAUTH_CALLBACK_QUERY,
    OrganizationMembershipDenied,
    clear_oauth_state_cookie,
    dashboard_session_is_owner,
    dashboard_session_login,
    github_organization_member_login,
    install_oauth_callback_query_redaction,
    oauth_state_cookie_name,
    require_dashboard_auth_configuration,
    sign_dashboard_session,
)
from management_portal.settings import PortalSettings

ASSETS_DIRECTORY = Path(__file__).parent / "assets"
PORTAL_HTML = (ASSETS_DIRECTORY / "index.html").read_text(encoding="utf-8")
SETTINGS_HTML = (ASSETS_DIRECTORY / "settings.html").read_text(encoding="utf-8")
INFERENCE_HTML = (ASSETS_DIRECTORY / "inference.html").read_text(encoding="utf-8")
InferenceInstanceId = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:gpu|cpu)(?:-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)?$",
    ),
]


class AgentConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completion_timeout_seconds: int = Field(strict=True, ge=1, le=90)


class AgentConfigRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    config: AgentConfigPayload
    version: int = Field(strict=True, ge=0)
    updated_at: str | None
    updated_by: str | None


class InferenceInstancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )


class InferenceInstanceCreatePayload(InferenceInstancePayload):
    instance_id: InferenceInstanceId
    context_size: int = Field(strict=True, ge=512, le=16384)
    threads: int = Field(strict=True, ge=1, le=8)

    @model_validator(mode="after")
    def validate_profile_limits(self) -> "InferenceInstanceCreatePayload":
        if self.instance_id.startswith("cpu") and self.context_size > 4096:
            raise ValueError("context_size exceeds the CPU instance profile limit")
        if self.instance_id.startswith("cpu") and self.threads > 4:
            raise ValueError("threads exceeds the CPU instance profile limit")
        return self


class InferenceInstanceConfigRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instance_id: InferenceInstanceId
    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    context_size: int = Field(strict=True, ge=512, le=16384)
    threads: int = Field(strict=True, ge=1, le=8)
    rollout_status: Literal[
        "not_requested",
        "pending",
        "rolling_out",
        "ready",
        "failed",
    ] = "not_requested"
    rollout_request_id: str | None = None
    rollout_error: str | None = None
    workload_status: Literal[
        "provisioning", "ready", "deleting", "failed", "deleted"
    ] = "ready"
    workload_error: str | None = None


class InferenceInstanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    config: InferenceInstanceConfigRecord
    version: int = Field(ge=0)
    updated_at: str | None
    updated_by: str | None


class InferenceInstancesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instances: list[InferenceInstanceRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_instances(self) -> InferenceInstancesResponse:
        instance_ids = [instance.config.instance_id for instance in self.instances]
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("inference instance IDs must be unique")
        return self


class AvailableModelFilenamesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    models: list[str]

    @model_validator(mode="after")
    def validate_models(self) -> AvailableModelFilenamesResponse:
        if len(set(self.models)) != len(self.models):
            raise ValueError("model filenames must be unique")
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf", filename)
            for filename in self.models
        ):
            raise ValueError("model filename is invalid")
        return self


class ModelDownloadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_filename: str = Field(
        min_length=6,
        max_length=205,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$",
    )
    model_url: str = Field(max_length=4096)


class ModelDownloadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    model_filename: str
    requested_by: str
    status: Literal["queued", "downloading", "verifying", "ready", "failed"]
    public_error: str | None
    attempts: int
    created_at: str
    updated_at: str


class InferenceMetricValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: float | None
    status: Literal["available", "stale", "unavailable"]


class InferenceInstanceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instance_id: InferenceInstanceId
    cpu_utilization_percent: InferenceMetricValue
    ram_used_bytes: InferenceMetricValue
    gpu_utilization_percent: InferenceMetricValue
    vram_used_bytes: InferenceMetricValue
    vram_total_bytes: InferenceMetricValue
    sampled_at: str | None


class InferenceMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instances: list[InferenceInstanceMetrics]


class AgentProfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    prompt_content: str = Field(default="", max_length=4000)
    model_profile: Literal["local_llama_cpp"]
    skill_ids: list[str] = Field(default_factory=list, max_length=20)
    tool_profile: Literal[
        "gather_context",
        "create_plan",
        "assess_risk",
        "prepare_changes",
        "repair_changes",
    ]

    @model_validator(mode="after")
    def validate_skill_ids(self) -> AgentProfileRecord:
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", skill_id)
            for skill_id in self.skill_ids
        ):
            raise ValueError("skill_ids must contain valid identifiers")
        if len(set(self.skill_ids)) != len(self.skill_ids):
            raise ValueError("skill_ids must be unique")
        return self


class AgentRosterRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agents: list[AgentProfileRecord] = Field(min_length=1, max_length=20)
    assignments: dict[str, str]

    @model_validator(mode="after")
    def validate_assignments(self) -> AgentRosterRecord:
        role_tool_profiles = {
            "context": "gather_context",
            "planning": "create_plan",
            "risk": "assess_risk",
            "implementation": "prepare_changes",
            "repair": "repair_changes",
        }
        if set(self.assignments) != set(role_tool_profiles):
            raise ValueError("assignments must include every supported workflow role")
        agents_by_id = {agent.agent_id: agent for agent in self.agents}
        if len(agents_by_id) != len(self.agents):
            raise ValueError("agent_id values must be unique")
        for role, agent_id in self.assignments.items():
            profile = agents_by_id.get(agent_id)
            if profile is None:
                raise ValueError(f"assignment for {role} references an unknown agent")
            if profile.tool_profile != role_tool_profiles[role]:
                raise ValueError(f"tool_profile is not allowed for the {role} role")
        return self


class AgentRosterUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    roster: AgentRosterRecord
    expected_revision: int = Field(ge=0)


class AgentRosterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    roster: AgentRosterRecord | None
    revision: int = Field(ge=0)
    updated_at: str | None
    updated_by: str | None


class NoCIResolutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    actor: str = Field(min_length=1, max_length=39)
    decided_at: str = Field(min_length=1, max_length=40)
    decision: Literal["proceed_without_ci"]
    pull_request_id: str = Field(min_length=1, max_length=200)


class ExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    thread_id: str = Field(min_length=1, max_length=200)
    checkpoint_id: str | None = Field(default=None, max_length=200)
    created_at: str | None = Field(default=None, max_length=40)
    step: int | None = None
    workflow_status: str | None = Field(default=None, max_length=160)
    waiting_for: str | None = Field(default=None, max_length=160)
    task_status: str | None = Field(default=None, max_length=160)
    event_type: str | None = Field(default=None, max_length=160)
    event_status: str | None = Field(default=None, max_length=160)
    next_nodes: list[str] = Field(default_factory=list, max_length=14)
    no_ci_resolution: NoCIResolutionSummary | None = None


class ExecutionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executions: list[ExecutionSummary]


class ExecutionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    checkpoints: list[ExecutionSummary]


class WorkflowSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str
    issue_number: None
    repository: str | None
    pull_request_number: int | None
    pull_request_id: str | None
    latest_event: Literal["ready", "ci", "merged", "review_changes_requested"]
    latest_event_at: str
    delivery_id: str
    delivery_status: Literal["processing", "accepted", "failed", "unavailable"]
    status: Literal["queued", "processing", "processed", "failed", "unavailable"]
    workflow_status: str | None = Field(default=None, max_length=160)
    waiting_for: str | None = Field(default=None, max_length=160)
    attempts: int
    has_failure: bool
    pull_request_merged: bool | None
    ci_status: Literal["unknown", "success", "pending", "failure", "unavailable"]
    ci_complete: bool | None


class WorkflowList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workflows: list[WorkflowSummary]


async def _internal_service_request(
    request: Request,
    settings: PortalSettings,
    *,
    service_url: str,
    transport_name: str,
    method: str,
    path: str,
    unavailable_detail: str,
    not_found_detail: str | None = None,
    conflict_detail: str | None = None,
    unprocessable_detail: str | None = None,
    json_body: dict[str, object] | None = None,
) -> dict[str, object]:
    if not settings.internal_service_token or not service_url:
        raise HTTPException(status_code=503, detail=unavailable_detail)
    transport = getattr(request.app.state, transport_name, None)
    try:
        async with httpx.AsyncClient(
            base_url=service_url.rstrip("/"),
            transport=transport,
            timeout=10.0,
        ) as client:
            response = await client.request(
                method,
                path,
                headers={
                    "Authorization": f"Bearer {settings.internal_service_token}",
                    "Accept": "application/json",
                },
                json=json_body,
            )
        if response.status_code == 404 and not_found_detail:
            raise HTTPException(status_code=404, detail=not_found_detail)
        if response.status_code == 409 and conflict_detail:
            raise HTTPException(status_code=409, detail=conflict_detail)
        if response.status_code == 422 and unprocessable_detail:
            raise HTTPException(status_code=422, detail=unprocessable_detail)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid workflow response")
        return payload
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise HTTPException(status_code=503, detail=unavailable_detail) from error


async def _langgraph_request(
    request: Request,
    settings: PortalSettings,
    *,
    method: str,
    path: str,
    not_found_detail: str = "execution not found",
    conflict_detail: str | None = None,
    unprocessable_detail: str | None = None,
    json_body: dict[str, object] | None = None,
) -> dict[str, object]:
    return await _internal_service_request(
        request,
        settings,
        service_url=settings.langgraph_service_url,
        transport_name="langgraph_http_transport",
        method=method,
        path=path,
        unavailable_detail="workflow service unavailable",
        not_found_detail=not_found_detail,
        conflict_detail=conflict_detail,
        unprocessable_detail=unprocessable_detail,
        json_body=json_body,
    )


async def _github_request(
    request: Request,
    settings: PortalSettings,
    *,
    path: str,
    method: str = "GET",
    conflict_detail: str | None = None,
    json_body: dict[str, object] | None = None,
) -> dict[str, object]:
    return await _internal_service_request(
        request,
        settings,
        service_url=settings.github_service_url,
        transport_name="github_http_transport",
        method=method,
        path=path,
        unavailable_detail="workflow state unavailable",
        conflict_detail=conflict_detail,
        json_body=json_body,
    )


def _dashboard_login(request: Request, settings: PortalSettings) -> str:
    require_dashboard_auth_configuration(settings)
    login = dashboard_session_login(request, settings)
    if login is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return login


def _require_same_origin_https(request: Request) -> None:
    origin = urlsplit(request.headers.get("origin", ""))
    host = request.headers.get("host", "")
    if (
        origin.scheme != "https"
        or not origin.netloc
        or origin.netloc.casefold() != host.casefold()
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise HTTPException(status_code=403, detail="same-origin request required")


def register_management_portal(app: FastAPI, settings: PortalSettings) -> None:
    install_oauth_callback_query_redaction(app)

    @app.exception_handler(RequestValidationError)
    async def redact_download_validation_error(request: Request, error):
        if request.url.path.endswith("/model-downloads"):
            return JSONResponse(
                status_code=422,
                content={"detail": "model download request was rejected"},
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def prevent_management_api_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    router = APIRouter()

    @router.get("/auth/login")
    async def dashboard_login() -> RedirectResponse:
        require_dashboard_auth_configuration(settings)
        state = secrets.token_urlsafe(32)
        location = f"{GITHUB_OAUTH_AUTHORIZE_URL}?{
            urlencode(
                {
                    'client_id': settings.github_oauth_client_id,
                    'redirect_uri': settings.github_oauth_callback_url,
                    'scope': 'read:org',
                    'state': state,
                }
            )
        }"
        response = RedirectResponse(location, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            oauth_state_cookie_name(state),
            state,
            max_age=DASHBOARD_STATE_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/auth",
        )
        return response

    @router.get("/auth")
    async def dashboard_oauth_callback(request: Request):
        require_dashboard_auth_configuration(settings)
        query = OAUTH_CALLBACK_QUERY.get() or {}
        state_values = query.get("state", [])
        state = state_values[0] if len(state_values) == 1 else ""
        cookie_state = (
            request.cookies.get(oauth_state_cookie_name(state), "") if state else ""
        )
        if (
            not state
            or not cookie_state
            or not hmac.compare_digest(
                cookie_state.encode("utf-8"), state.encode("utf-8")
            )
        ):
            raise HTTPException(status_code=400, detail="invalid OAuth state")

        if query.get("error"):
            response = PlainTextResponse(
                "GitHub authorization was not completed.", status_code=401
            )
            clear_oauth_state_cookie(response, state)
            return response

        code_values = query.get("code", [])
        if len(code_values) != 1 or not code_values[0]:
            raise HTTPException(status_code=400, detail="invalid OAuth response")

        try:
            login, is_owner = await github_organization_member_login(
                request,
                settings=settings,
                code=code_values[0],
            )
        except OrganizationMembershipDenied:
            response = PlainTextResponse(
                "Active membership in the approved GitHub organization is required.",
                status_code=403,
            )
            clear_oauth_state_cookie(response, state)
            return response
        except Exception:
            response = PlainTextResponse(
                "GitHub authorization is temporarily unavailable.", status_code=502
            )
            clear_oauth_state_cookie(response, state)
            return response

        response = RedirectResponse("/", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            DASHBOARD_SESSION_COOKIE,
            sign_dashboard_session(
                login,
                settings.dashboard_session_signing_key or "",
                is_owner=is_owner,
            ),
            max_age=DASHBOARD_SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        clear_oauth_state_cookie(response, state)
        return response

    @router.post("/auth/logout")
    async def dashboard_logout(request: Request) -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            DASHBOARD_SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        for cookie_name in request.cookies:
            if cookie_name.startswith(f"{DASHBOARD_STATE_COOKIE}_"):
                response.delete_cookie(
                    cookie_name,
                    path="/auth",
                    secure=True,
                    httponly=True,
                    samesite="lax",
                )
        return response

    @router.get("/", response_class=HTMLResponse)
    async def workflow_dashboard(request: Request):
        require_dashboard_auth_configuration(settings)
        if dashboard_session_login(request, settings) is None:
            return RedirectResponse("/auth/login", status_code=303)
        return HTMLResponse(PORTAL_HTML)

    @router.get("/settings", response_class=HTMLResponse)
    async def agent_configuration_page(request: Request):
        require_dashboard_auth_configuration(settings)
        if dashboard_session_login(request, settings) is None:
            return RedirectResponse("/auth/login", status_code=303)
        return HTMLResponse(SETTINGS_HTML)

    @router.get("/inference", response_class=HTMLResponse)
    async def inference_configuration_page(request: Request):
        require_dashboard_auth_configuration(settings)
        if dashboard_session_login(request, settings) is None:
            return RedirectResponse("/auth/login", status_code=303)
        return HTMLResponse(INFERENCE_HTML)

    @router.get("/api/workflows")
    async def workflow_state(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        try:
            payload = await _github_request(
                request,
                settings,
                path="/internal/management/workflows",
            )
            workflows = WorkflowList.model_validate(payload)
            workflow_summaries = workflows.workflows[:100]
            execution_payload = await _langgraph_request(
                request,
                settings,
                method="GET",
                path="/internal/management/executions?limit=100",
            )
            executions = ExecutionList.model_validate(execution_payload)
            execution_by_thread_id = {
                execution.thread_id: execution for execution in executions.executions
            }

            def project_workflow(workflow: WorkflowSummary) -> dict[str, object]:
                execution = execution_by_thread_id.get(workflow.task_id)
                return {
                    **workflow.model_dump(mode="json"),
                    "workflow_status": (
                        execution.workflow_status if execution else None
                    ),
                    "waiting_for": execution.waiting_for if execution else None,
                }

            return {
                "workflows": [
                    project_workflow(workflow) for workflow in workflow_summaries
                ]
            }
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow state unavailable"
            ) from error

    @router.post("/api/workflows/{task_id}/retry")
    async def retry_workflow(request: Request, task_id: str) -> dict[str, bool]:
        _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        if not task_id or len(task_id) > 200:
            raise HTTPException(status_code=404, detail="workflow not found")
        result = await _github_request(
            request,
            settings,
            method="POST",
            path=f"/internal/management/workflows/{quote(task_id, safe='')}/retry",
            conflict_detail="workflow is not eligible for retry",
        )
        if result.get("requeued") is not True:
            raise HTTPException(status_code=503, detail="workflow retry unavailable")
        return {"requeued": True}

    @router.get("/api/agent-config")
    async def agent_config(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/agent-config",
        )
        try:
            record = AgentConfigRecord.model_validate(payload)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow service unavailable"
            ) from error
        return {
            **record.model_dump(mode="json"),
            "can_edit": dashboard_session_is_owner(request, settings),
        }

    @router.put("/api/agent-config")
    async def update_agent_config(
        request: Request,
        payload: AgentConfigPayload,
    ) -> dict[str, object]:
        login = _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        response = await _langgraph_request(
            request,
            settings,
            method="PUT",
            path="/internal/management/agent-config",
            json_body={
                **payload.model_dump(),
                "updated_by": login,
            },
        )
        try:
            record = AgentConfigRecord.model_validate(response)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow service unavailable"
            ) from error
        return record.model_dump(mode="json")

    @router.get("/api/inference-instances")
    async def inference_instances(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-instances",
        )
        try:
            record = InferenceInstancesResponse.model_validate(payload)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error
        return {
            **record.model_dump(mode="json"),
            "can_edit": True,
        }

    @router.post("/api/inference-instances")
    async def create_inference_instance(
        request: Request,
        payload: InferenceInstanceCreatePayload,
    ) -> dict[str, object]:
        login = _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        available_payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-models",
        )
        try:
            available_models = AvailableModelFilenamesResponse.model_validate(
                available_payload
            ).models
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="available model listing unavailable"
            ) from error
        if payload.model_filename not in available_models:
            raise HTTPException(
                status_code=422, detail="selected model is not present in storage"
            )
        response = await _langgraph_request(
            request,
            settings,
            method="POST",
            path="/internal/management/inference-instances",
            conflict_detail="inference instance already exists",
            unprocessable_detail="inference instance configuration was rejected",
            json_body={
                **payload.model_dump(),
                "created_by": login,
            },
        )
        try:
            return InferenceInstanceRecord.model_validate(response).model_dump(
                mode="json"
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error

    @router.delete("/api/inference-instances/{instance_id}")
    async def delete_inference_instance(
        request: Request,
        instance_id: InferenceInstanceId,
    ) -> dict[str, object]:
        _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        return await _langgraph_request(
            request,
            settings,
            method="DELETE",
            path=f"/internal/management/inference-instances/{instance_id}",
            not_found_detail="inference instance not found",
            conflict_detail=("inference instance cannot be deleted while active"),
            unprocessable_detail="inference instance deletion was rejected",
        )

    @router.get("/api/inference-models")
    async def inference_models(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-models",
        )
        try:
            return AvailableModelFilenamesResponse.model_validate(payload).model_dump(
                mode="json"
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="available model listing unavailable"
            ) from error

    @router.put("/api/inference-instances/{instance_id}")
    async def update_inference_instance(
        request: Request,
        instance_id: InferenceInstanceId,
        payload: InferenceInstancePayload,
    ) -> dict[str, object]:
        login = _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        available_payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-models",
        )
        try:
            available_models = AvailableModelFilenamesResponse.model_validate(
                available_payload
            ).models
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="available model listing unavailable"
            ) from error
        if payload.model_filename not in available_models:
            raise HTTPException(
                status_code=422, detail="selected model is not present in storage"
            )
        response = await _langgraph_request(
            request,
            settings,
            method="PUT",
            path=f"/internal/management/inference-instances/{instance_id}",
            conflict_detail="inference model download or rollout is active",
            unprocessable_detail="inference model selection was rejected",
            json_body={
                "instance_id": instance_id,
                **payload.model_dump(),
                "updated_by": login,
            },
        )
        try:
            return InferenceInstanceRecord.model_validate(response).model_dump(
                mode="json"
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference configuration unavailable"
            ) from error

    @router.get("/api/agent-roster")
    async def agent_roster(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/agent-roster",
        )
        try:
            record = AgentRosterResponse.model_validate(payload)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow service unavailable"
            ) from error
        return {
            **record.model_dump(mode="json"),
            "can_edit": dashboard_session_is_owner(request, settings),
        }

    @router.put("/api/agent-roster")
    async def update_agent_roster(
        request: Request,
        payload: AgentRosterUpdatePayload,
    ) -> dict[str, object]:
        login = _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        response = await _langgraph_request(
            request,
            settings,
            method="PUT",
            path="/internal/management/agent-roster",
            conflict_detail="agent roster changed; reload before saving",
            unprocessable_detail="agent roster was rejected by the workflow service",
            json_body={
                "roster": payload.roster.model_dump(mode="json"),
                "expected_revision": payload.expected_revision,
                "updated_by": login,
            },
        )
        try:
            record = AgentRosterResponse.model_validate(response)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow service unavailable"
            ) from error
        return record.model_dump(mode="json")

    @router.post("/api/inference-model-downloads")
    async def request_inference_model_download(
        request: Request,
        payload: ModelDownloadPayload,
    ) -> dict[str, object]:
        login = _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        response = await _langgraph_request(
            request,
            settings,
            method="POST",
            path="/internal/management/inference-model-downloads",
            conflict_detail="a shared model download is already active",
            unprocessable_detail="model download request was rejected",
            json_body={**payload.model_dump(), "requested_by": login},
        )
        try:
            return ModelDownloadRecord.model_validate(response).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="model download status unavailable"
            ) from error

    @router.get("/api/inference-model-downloads/{request_id}")
    async def inference_model_download_status(
        request: Request, request_id: UUID
    ) -> dict[str, object]:
        _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        response = await _langgraph_request(
            request,
            settings,
            method="GET",
            path=f"/internal/management/inference-model-downloads/{request_id}",
            not_found_detail="model download not found",
        )
        try:
            return ModelDownloadRecord.model_validate(response).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="model download status unavailable"
            ) from error

    @router.get("/api/inference-metrics")
    async def inference_metrics(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        response = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-metrics",
        )
        try:
            return InferenceMetricsResponse.model_validate(response).model_dump(
                mode="json"
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference metrics unavailable"
            ) from error

    @router.get("/api/executions")
    async def executions(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/executions",
        )
        try:
            return ExecutionList.model_validate(payload).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow service unavailable"
            ) from error

    @router.get("/api/executions/{thread_id}")
    async def execution_detail(request: Request, thread_id: str) -> dict[str, object]:
        _dashboard_login(request, settings)
        if not thread_id or len(thread_id) > 200:
            raise HTTPException(status_code=404, detail="execution not found")
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path=f"/internal/management/executions/{quote(thread_id, safe='')}",
        )
        try:
            return ExecutionDetail.model_validate(payload).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow service unavailable"
            ) from error

    @router.post("/api/executions/{thread_id}/resolve-no-ci")
    async def resolve_execution_without_ci(
        request: Request, thread_id: str
    ) -> dict[str, bool]:
        login = _dashboard_login(request, settings)
        if not dashboard_session_is_owner(request, settings):
            raise HTTPException(status_code=403, detail="organization owner required")
        _require_same_origin_https(request)
        if not thread_id or len(thread_id) > 200:
            raise HTTPException(status_code=404, detail="execution not found")
        result = await _langgraph_request(
            request,
            settings,
            method="POST",
            path=(
                f"/internal/management/executions/{quote(thread_id, safe='')}"
                "/resolve-no-ci"
            ),
            conflict_detail="execution has no unresolved CI gap",
            json_body={"actor": login, "decision": "proceed_without_ci"},
        )
        if result.get("resolved") is not True:
            raise HTTPException(status_code=503, detail="CI resolution unavailable")
        return {"resolved": True}

    app.include_router(router)
    app.mount(
        "/management-portal/assets",
        StaticFiles(directory=ASSETS_DIRECTORY),
        name="management-portal-assets",
    )
