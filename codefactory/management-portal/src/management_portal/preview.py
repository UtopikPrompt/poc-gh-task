"""Loopback-only, read-only portal preview backed by live service APIs."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from management_portal.router import (
    ASSETS_DIRECTORY,
    PORTAL_HTML,
    SETTINGS_HTML,
    AgentConfigRecord,
    AgentRosterResponse,
    AvailableModelFilenamesResponse,
    ExecutionDetail,
    ExecutionList,
    InferenceMetricsResponse,
    InferenceInstancesResponse,
    WorkflowList,
    _github_request,
    _langgraph_request,
)
from management_portal.settings import PortalSettings


def _is_loopback_client(host: str | None) -> bool:
    if host == "testclient":
        return True
    try:
        return ip_address(host).is_loopback
    except (TypeError, ValueError):
        return False


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    settings = settings or PortalSettings.from_environment()
    app = FastAPI(title="Codefactory Management Portal Local Preview")

    @app.middleware("http")
    async def restrict_preview_to_loopback(request: Request, call_next):
        client_host = request.client.host if request.client else None
        if not _is_loopback_client(client_host):
            return JSONResponse(
                {"detail": "Local preview is available only from this machine"},
                status_code=403,
            )
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path in {"/", "/settings"} or request.url.path.startswith(
            "/management-portal/assets/"
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(PORTAL_HTML)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> HTMLResponse:
        return HTMLResponse(SETTINGS_HTML)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/auth/logout")
    async def logout() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.get("/api/workflows")
    async def workflows(request: Request) -> dict[str, object]:
        payload = await _github_request(
            request,
            settings,
            path="/internal/management/workflows",
        )
        try:
            return WorkflowList.model_validate(payload).model_dump(mode="json")
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow state unavailable"
            ) from error

    @app.get("/api/agent-config")
    async def get_agent_config(request: Request) -> dict[str, object]:
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
            "can_edit": False,
            "preview": True,
        }

    @app.get("/api/inference-instances")
    async def get_inference_instances(request: Request) -> dict[str, object]:
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
        return {**record.model_dump(mode="json"), "can_edit": False, "preview": True}

    @app.get("/api/inference-models")
    async def get_inference_models(request: Request) -> dict[str, object]:
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-models",
        )
        try:
            record = AvailableModelFilenamesResponse.model_validate(payload)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="available model listing unavailable"
            ) from error
        return {**record.model_dump(mode="json"), "preview": True}

    @app.get("/api/inference-metrics")
    async def get_inference_metrics(request: Request) -> dict[str, object]:
        payload = await _langgraph_request(
            request,
            settings,
            method="GET",
            path="/internal/management/inference-metrics",
        )
        try:
            return InferenceMetricsResponse.model_validate(payload).model_dump(
                mode="json"
            )
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="inference metrics unavailable"
            ) from error

    @app.get("/api/agent-roster")
    async def get_agent_roster(request: Request) -> dict[str, object]:
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
            "can_edit": False,
            "preview": True,
        }

    @app.put("/api/agent-config")
    async def update_agent_config() -> None:
        raise HTTPException(status_code=403, detail="local preview is read-only")

    @app.put("/api/inference-instances/{instance_id}")
    async def update_inference_instance() -> None:
        raise HTTPException(status_code=403, detail="local preview is read-only")

    @app.post("/api/inference-model-downloads")
    async def request_inference_model_download() -> None:
        raise HTTPException(status_code=403, detail="local preview is read-only")

    @app.get("/api/inference-model-downloads/{request_id}")
    async def get_inference_model_download_status() -> None:
        raise HTTPException(status_code=403, detail="local preview is read-only")

    @app.put("/api/agent-roster")
    async def update_agent_roster() -> None:
        raise HTTPException(status_code=403, detail="local preview is read-only")

    @app.get("/api/executions")
    async def executions(request: Request) -> dict[str, object]:
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

    @app.get("/api/executions/{thread_id}", response_model=None)
    async def execution_detail(
        request: Request, thread_id: str
    ) -> dict[str, object] | JSONResponse:
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

    app.mount(
        "/management-portal/assets",
        StaticFiles(directory=ASSETS_DIRECTORY),
        name="management-portal-assets",
    )
    return app
