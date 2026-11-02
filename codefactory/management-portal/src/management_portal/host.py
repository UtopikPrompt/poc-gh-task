"""FastAPI application entrypoint for the management portal."""

from fastapi import FastAPI

from management_portal.router import register_management_portal
from management_portal.settings import PortalSettings


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    settings = settings or PortalSettings.from_environment()
    app = FastAPI(title="Codefactory Management Portal")
    register_management_portal(app, settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
