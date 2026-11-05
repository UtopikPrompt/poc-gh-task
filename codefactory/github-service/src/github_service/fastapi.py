"""FastAPI service for authenticated, checkpointed GitHub webhook intake."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from github_service.settings import RuntimeSettings
from github_service.publishing import FileChangeProposal
from github_service.webhook_service import WebhookService
from github_service.webhooks import (
    WebhookAuthenticationError,
    decode_github_webhook,
)


class InternalStore(Protocol):
    def list_workflows(self) -> list[dict[str, object]]: ...

    def requeue_failed_task(self, task_id: str) -> bool: ...

    def claim_event(
        self,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 8,
    ) -> dict[str, object] | None: ...

    def acknowledge_event(self, event_id: str, claim_token: str) -> bool: ...

    def retry_event(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        max_attempts: int = 8,
    ) -> bool: ...


@dataclass(frozen=True)
class InternalServiceRuntime:
    store: InternalStore
    token: str
    lease_seconds: int = 60
    max_attempts: int = 8
    complete_task: (
        Callable[[str, Mapping[str, object] | None], dict[str, object]] | None
    ) = None
    start_task: Callable[[str], dict[str, object]] | None = None
    propose_file_changes: Callable[[FileChangeProposal], dict[str, object]] | None = (
        None
    )
    get_source_tree: Callable[[str], dict[str, object]] | None = None
    get_source_files: Callable[..., dict[str, object]] | None = None
    request_human_feedback: (
        Callable[[str, str, str, int | None], dict[str, object]] | None
    ) = None


MAX_PROPOSAL_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SOURCE_REQUEST_BYTES = 8 * 1024


class WebhookRuntime:
    def __init__(
        self,
        *,
        service: WebhookService,
        webhook_secret: str,
    ):
        self._service = service
        self._webhook_secret = webhook_secret

    def handle(
        self,
        *,
        raw_body: bytes,
        event_name: str,
        delivery_id: str,
        signature: str | None,
    ) -> dict[str, object]:
        webhook = decode_github_webhook(
            raw_body,
            event_name=event_name,
            delivery_id=delivery_id,
            signature=signature,
            secret=self._webhook_secret,
        )
        return self._service.handle(webhook)


def create_http_app(
    *,
    settings: RuntimeSettings,
    lifespan: Callable[..., object],
) -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/github")
    async def receive_github_webhook(request: Request) -> dict[str, object]:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_webhook_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="webhook payload too large",
                    )
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail="invalid content length"
                ) from error
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.max_webhook_bytes:
                raise HTTPException(status_code=413, detail="webhook payload too large")
            body.extend(chunk)
        raw_body = bytes(body)
        try:
            return await run_in_threadpool(
                request.app.state.webhook_runtime.handle,
                raw_body=raw_body,
                event_name=request.headers.get("x-github-event", ""),
                delivery_id=request.headers.get("x-github-delivery", ""),
                signature=request.headers.get("x-hub-signature-256"),
            )
        except WebhookAuthenticationError as error:
            raise HTTPException(
                status_code=401, detail="invalid webhook signature"
            ) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=500, detail="webhook processing failed"
            ) from error

    def internal_runtime(request: Request) -> InternalServiceRuntime:
        runtime = getattr(request.app.state, "internal_runtime", None)
        if not isinstance(runtime, InternalServiceRuntime) or not runtime.token:
            raise HTTPException(status_code=503, detail="internal API unavailable")
        expected = f"Bearer {runtime.token}"
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")
        return runtime

    @app.get("/internal/management/workflows")
    async def list_management_workflows(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        try:
            workflows = await run_in_threadpool(runtime.store.list_workflows)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow state unavailable"
            ) from error
        return {"workflows": workflows}

    @app.post("/internal/management/workflows/{task_id}/retry")
    async def retry_management_workflow(
        task_id: str, request: Request
    ) -> dict[str, bool]:
        runtime = internal_runtime(request)
        if not task_id or len(task_id) > 200:
            raise HTTPException(status_code=404, detail="workflow not found")
        requeued = await run_in_threadpool(
            runtime.store.requeue_failed_task,
            task_id,
        )
        if not requeued:
            raise HTTPException(
                status_code=409,
                detail="workflow is not eligible for retry",
            )
        return {"requeued": True}

    @app.post("/internal/events/claim")
    async def claim_event(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        event = await run_in_threadpool(
            runtime.store.claim_event,
            lease_seconds=runtime.lease_seconds,
            max_attempts=runtime.max_attempts,
        )
        return {"event": event}

    @app.post("/internal/events/{event_id}/ack")
    async def acknowledge_event(event_id: str, request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        payload = await request.json()
        claim_token = payload.get("claim_token") if isinstance(payload, dict) else None
        if not isinstance(claim_token, str) or not claim_token:
            raise HTTPException(status_code=400, detail="claim_token is required")
        acknowledged = await run_in_threadpool(
            runtime.store.acknowledge_event,
            event_id,
            claim_token,
        )
        if not acknowledged:
            raise HTTPException(
                status_code=409, detail="event claim is no longer valid"
            )
        return {"acknowledged": True}

    @app.post("/internal/events/{event_id}/retry")
    async def retry_event(event_id: str, request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        payload = await request.json()
        claim_token = payload.get("claim_token") if isinstance(payload, dict) else None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(claim_token, str) or not claim_token:
            raise HTTPException(status_code=400, detail="claim_token is required")
        if not isinstance(error, str):
            raise HTTPException(status_code=400, detail="error is required")
        retried = await run_in_threadpool(
            runtime.store.retry_event,
            event_id,
            claim_token,
            error,
            max_attempts=runtime.max_attempts,
        )
        if not retried:
            raise HTTPException(
                status_code=409, detail="event claim is no longer valid"
            )
        return {"retried": True}

    @app.post("/internal/github/propose-changes")
    async def propose_file_changes(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.propose_file_changes is None:
            raise HTTPException(status_code=503, detail="file proposal unavailable")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_PROPOSAL_REQUEST_BYTES:
                    raise HTTPException(
                        status_code=413, detail="proposal payload too large"
                    )
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail="invalid content length"
                ) from error
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_PROPOSAL_REQUEST_BYTES:
                raise HTTPException(
                    status_code=413, detail="proposal payload too large"
                )
            body.extend(chunk)
        try:
            proposal = FileChangeProposal.from_payload(json.loads(body))
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            return await run_in_threadpool(runtime.propose_file_changes, proposal)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/internal/github/source-tree")
    async def get_source_tree(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.get_source_tree is None:
            raise HTTPException(status_code=503, detail="source context unavailable")
        payload = await _bounded_json(request, MAX_SOURCE_REQUEST_BYTES)
        if not isinstance(payload, dict) or set(payload) != {"task_id"}:
            raise HTTPException(status_code=400, detail="request must contain task_id")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        try:
            return await run_in_threadpool(runtime.get_source_tree, task_id)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/internal/github/source-files")
    async def get_source_files(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.get_source_files is None:
            raise HTTPException(status_code=503, detail="source context unavailable")
        payload = await _bounded_json(request, MAX_SOURCE_REQUEST_BYTES)
        allowed_fields = {
            "task_id",
            "paths",
            "pull_request_number",
            "expected_pull_request_id",
        }
        if (
            not isinstance(payload, dict)
            or not {"task_id", "paths"}.issubset(payload)
            or set(payload) - allowed_fields
        ):
            raise HTTPException(status_code=400, detail="invalid source files request")
        task_id = payload.get("task_id")
        pull_request_number = payload.get("pull_request_number")
        expected_pull_request_id = payload.get("expected_pull_request_id")
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        if (pull_request_number is None) != (expected_pull_request_id is None):
            raise HTTPException(status_code=400, detail="invalid pull request identity")
        if pull_request_number is not None and (
            not isinstance(pull_request_number, int)
            or isinstance(pull_request_number, bool)
            or pull_request_number <= 0
            or not isinstance(expected_pull_request_id, str)
            or not expected_pull_request_id.isdecimal()
            or int(expected_pull_request_id) <= 0
        ):
            raise HTTPException(status_code=400, detail="invalid pull request identity")
        try:
            if pull_request_number is not None:
                return await run_in_threadpool(
                    runtime.get_source_files,
                    task_id,
                    payload.get("paths"),
                    pull_request_number,
                    expected_pull_request_id,
                )
            return await run_in_threadpool(
                runtime.get_source_files, task_id, payload.get("paths")
            )
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/internal/github/request-human-feedback")
    async def request_human_feedback(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.request_human_feedback is None:
            raise HTTPException(status_code=503, detail="PR conversation unavailable")
        payload = await _bounded_json(request, 8 * 1024)
        if (
            not isinstance(payload, dict)
            or not {"task_id", "question", "dedupe_key"}.issubset(payload)
            or set(payload)
            - {"task_id", "question", "dedupe_key", "source_review_comment_id"}
        ):
            raise HTTPException(status_code=400, detail="invalid feedback request")
        task_id = payload.get("task_id")
        question = payload.get("question")
        dedupe_key = payload.get("dedupe_key")
        source_review_comment_id = payload.get("source_review_comment_id")
        if (
            not isinstance(task_id, str)
            or not isinstance(question, str)
            or not isinstance(dedupe_key, str)
            or (
                source_review_comment_id is not None
                and (
                    not isinstance(source_review_comment_id, int)
                    or isinstance(source_review_comment_id, bool)
                    or source_review_comment_id <= 0
                )
            )
        ):
            raise HTTPException(status_code=400, detail="invalid feedback request")
        try:
            return await run_in_threadpool(
                runtime.request_human_feedback,
                task_id,
                question,
                dedupe_key,
                source_review_comment_id,
            )
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/internal/github/complete-task")
    async def complete_task(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.complete_task is None:
            raise HTTPException(status_code=503, detail="completion unavailable")
        payload = await request.json()
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        resolution = (
            payload.get("no_ci_resolution") if isinstance(payload, dict) else None
        )
        if (
            not isinstance(payload, dict)
            or set(payload) - {"task_id", "no_ci_resolution"}
            or (resolution is not None and not isinstance(resolution, Mapping))
        ):
            raise HTTPException(status_code=400, detail="invalid completion request")
        return await run_in_threadpool(runtime.complete_task, task_id, resolution)

    @app.post("/internal/github/start-task")
    async def start_task(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.start_task is None:
            raise HTTPException(status_code=503, detail="task start unavailable")
        payload = await request.json()
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        try:
            result = await run_in_threadpool(runtime.start_task, task_id)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if result.get("started") is not True:
            raise HTTPException(status_code=409, detail="task start was not confirmed")
        return result

    return app


async def _bounded_json(request: Request, maximum_bytes: int) -> object:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > maximum_bytes:
                raise HTTPException(status_code=413, detail="source request too large")
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="invalid content length"
            ) from error
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise HTTPException(status_code=413, detail="source request too large")
        body.extend(chunk)
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="invalid JSON") from error
