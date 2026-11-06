"""Read-only API for listing model artifacts in shared inference storage."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
import re
import stat

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from langgraph_service.model_download import validate_model_filename

_MODEL_FILENAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf$"


class AvailableModelFilenamesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    models: list[str]

    @model_validator(mode="after")
    def validate_models(self) -> AvailableModelFilenamesResponse:
        if len(set(self.models)) != len(self.models):
            raise ValueError("model filenames must be unique")
        for filename in self.models:
            validate_model_filename(filename)
        return self


def list_model_filenames(model_directory: str | Path) -> list[str]:
    directory = Path(model_directory)
    directory_stat = directory.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("model storage is not a directory")

    filenames = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not re.fullmatch(_MODEL_FILENAME_PATTERN, entry.name):
                continue
            if entry.is_file(follow_symlinks=False):
                filenames.append(entry.name)
    return sorted(filenames)


def create_app(
    *, model_directory: str | Path | None = None, service_token: str | None = None
) -> FastAPI:
    configured_directory = (
        str(model_directory)
        if model_directory is not None
        else os.environ.get("MODEL_DIRECTORY", "").strip()
    )
    token = (
        service_token
        if service_token is not None
        else os.environ.get("MODEL_ARTIFACT_API_TOKEN", "")
    )
    if not configured_directory or not token:
        raise ValueError("model artifact API settings are missing")
    directory = Path(configured_directory)

    app = FastAPI()

    def authenticate(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        try:
            list_model_filenames(directory)
        except OSError as error:
            raise HTTPException(
                status_code=503, detail="model storage is unavailable"
            ) from error
        return {"status": "ready"}

    @app.get(
        "/internal/models/available",
        response_model=AvailableModelFilenamesResponse,
        dependencies=[Depends(authenticate)],
    )
    async def available_models():
        try:
            return {"models": list_model_filenames(directory)}
        except OSError as error:
            raise HTTPException(
                status_code=503, detail="model storage is unavailable"
            ) from error

    return app
