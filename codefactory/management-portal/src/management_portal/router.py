"""HTTP routes and packaged UI for the management portal."""

from __future__ import annotations

import hmac
import httpx
from pathlib import Path
import secrets
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
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
    require_dashboard_auth_configuration,
    sign_dashboard_session,
)
from management_portal.settings import PortalSettings

ASSETS_DIRECTORY = Path(__file__).parent / "assets"
PORTAL_HTML = (ASSETS_DIRECTORY / "index.html").read_text(encoding="utf-8")


class AgentConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completion_timeout_seconds: int = Field(strict=True, ge=1, le=90)


class AgentConfigRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    config: AgentConfigPayload
    version: int = Field(strict=True, ge=0)
    updated_at: str | None
    updated_by: str | None


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
    latest_event: Literal["ready", "ci", "merged"]
    latest_event_at: str
    delivery_id: str
    delivery_status: Literal["processing", "accepted", "failed", "unavailable"]
    status: Literal["queued", "processing", "processed", "failed", "unavailable"]
    attempts: int
    has_failure: bool
    pull_request_merged: bool | None
    ci_status: Literal["unknown", "success", "pending", "failure"]
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
        not_found_detail="execution not found",
        json_body=json_body,
    )


async def _github_request(
    request: Request,
    settings: PortalSettings,
    *,
    path: str,
) -> dict[str, object]:
    return await _internal_service_request(
        request,
        settings,
        service_url=settings.github_service_url,
        transport_name="github_http_transport",
        method="GET",
        path=path,
        unavailable_detail="workflow state unavailable",
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
            DASHBOARD_STATE_COOKIE,
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
        cookie_state = request.cookies.get(DASHBOARD_STATE_COOKIE, "")
        if (
            len(state_values) != 1
            or not cookie_state
            or not hmac.compare_digest(
                cookie_state.encode("utf-8"), state_values[0].encode("utf-8")
            )
        ):
            raise HTTPException(status_code=400, detail="invalid OAuth state")

        if query.get("error"):
            response = PlainTextResponse(
                "GitHub authorization was not completed.", status_code=401
            )
            clear_oauth_state_cookie(response)
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
            clear_oauth_state_cookie(response)
            return response
        except Exception:
            response = PlainTextResponse(
                "GitHub authorization is temporarily unavailable.", status_code=502
            )
            clear_oauth_state_cookie(response)
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
        clear_oauth_state_cookie(response)
        return response

    @router.post("/auth/logout")
    async def dashboard_logout() -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            DASHBOARD_SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        clear_oauth_state_cookie(response)
        return response

    @router.get("/", response_class=HTMLResponse)
    async def workflow_dashboard(request: Request):
        require_dashboard_auth_configuration(settings)
        if dashboard_session_login(request, settings) is None:
            return RedirectResponse("/auth/login", status_code=303)
        return HTMLResponse(PORTAL_HTML)

    @router.get("/api/workflows")
    async def workflow_state(request: Request) -> dict[str, object]:
        _dashboard_login(request, settings)
        try:
            payload = await _github_request(
                request,
                settings,
                path="/internal/management/workflows",
            )
            return WorkflowList.model_validate(payload).model_dump(mode="json")
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow state unavailable"
            ) from error

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

    app.include_router(router)
    app.mount(
        "/management-portal/assets",
        StaticFiles(directory=ASSETS_DIRECTORY),
        name="management-portal-assets",
    )
