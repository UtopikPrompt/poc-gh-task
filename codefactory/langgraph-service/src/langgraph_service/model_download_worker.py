"""Standalone process for durable, leased inference model downloads."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import time

from langgraph_service.model_download import (
    ModelDownloadError,
    download_model_artifact,
)
from langgraph_service.storage.database import postgres_url_from_environment
from langgraph_service.storage.model_download_store import (
    FailureCode,
    PostgresModelDownloadStore,
)

logger = logging.getLogger(__name__)


def _failure_code(error: ModelDownloadError) -> FailureCode:
    message = str(error)
    if "SHA-256" in message:
        return "hash_mismatch"
    if "size limit" in message:
        return "too_large"
    if any(term in message for term in ("URL", "redirect", "resolved", "prohibited")):
        return "policy_rejected"
    return "download_failed"


def process_one(
    store: PostgresModelDownloadStore,
    *,
    model_directory: str | Path,
    lease_seconds: int = 120,
    downloader=download_model_artifact,
) -> bool:
    request = store.claim(lease_seconds=lease_seconds)
    if request is None:
        return False
    last_renewal = [time.monotonic()]

    def renew_lease() -> None:
        now = time.monotonic()
        if now - last_renewal[0] >= lease_seconds / 3:
            if not store.renew(
                request.request_id,
                request.lease_token,
                lease_seconds=lease_seconds,
            ):
                raise ModelDownloadError("download lease is no longer active")
            last_renewal[0] = now

    try:
        downloader(
            url=request.model_url,
            sha256=request.model_sha256,
            filename=request.model_filename,
            model_directory=model_directory,
            on_progress=renew_lease,
            on_verifying=lambda: store.mark_verifying(
                request.request_id, request.lease_token
            ),
        )
        store.complete(request.request_id, request.lease_token)
    except ModelDownloadError as error:
        try:
            store.fail(
                request.request_id,
                request.lease_token,
                code=_failure_code(error),
            )
        except Exception:
            logger.error("model download request could not be marked failed")
    except Exception:
        try:
            store.fail(
                request.request_id,
                request.lease_token,
                code="download_failed",
            )
        except Exception:
            logger.error("model download request could not be marked failed")
    return True


def main() -> None:
    database_url = postgres_url_from_environment("LANGGRAPH", required=True)
    model_directory = os.environ.get("MODEL_DIRECTORY", "").strip()
    if not model_directory:
        raise ValueError("MODEL_DIRECTORY must be configured")
    try:
        poll_seconds = float(os.environ.get("MODEL_DOWNLOAD_POLL_SECONDS", "5"))
        lease_seconds = int(os.environ.get("MODEL_DOWNLOAD_LEASE_SECONDS", "120"))
    except ValueError as error:
        raise ValueError("model download worker intervals are invalid") from error
    if poll_seconds <= 0 or lease_seconds < 30:
        raise ValueError("model download worker intervals must be positive")

    store = PostgresModelDownloadStore(database_url)
    while True:
        try:
            processed = process_one(
                store,
                model_directory=model_directory,
                lease_seconds=lease_seconds,
            )
        except Exception:
            logger.error("model download worker could not access its durable queue")
            processed = False
        if not processed:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
