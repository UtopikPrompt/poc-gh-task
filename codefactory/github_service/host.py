"""Operator-configured host entrypoint for bounded workflow operations."""

from __future__ import annotations

from fastapi import FastAPI

from codefactory.github_service.bootstrap import create_host_app


def create_app() -> FastAPI:
    """Preserve the documented Uvicorn factory while delegating composition."""
    return create_host_app()
