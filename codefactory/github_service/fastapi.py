"""FastAPI service for authenticated, checkpointed GitHub webhook intake."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from codefactory.github_service.settings import RuntimeSettings
from codefactory.github_service.webhook_service import WebhookService
from codefactory.github_service.webhooks import (
    WebhookAuthenticationError,
    decode_github_webhook,
)


class InternalStore(Protocol):
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
    complete_task: Callable[[str], dict[str, object]] | None = None
    publish_pull_request: Callable[..., dict[str, object]] | None = None


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

    @app.post("/internal/github/publish-pr")
    async def publish_pull_request(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.publish_pull_request is None:
            raise HTTPException(status_code=503, detail="PR publication unavailable")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid request")
        task_id = payload.get("task_id")
        fields = {name: payload.get(name) for name in ("title", "body", "head", "base")}
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        if any(
            not isinstance(value, str) or not value.strip() for value in fields.values()
        ):
            raise HTTPException(status_code=400, detail="PR metadata is incomplete")
        return await run_in_threadpool(
            runtime.publish_pull_request,
            task_id=task_id,
            **fields,
        )

    @app.post("/internal/github/complete-task")
    async def complete_task(request: Request) -> dict[str, object]:
        runtime = internal_runtime(request)
        if runtime.complete_task is None:
            raise HTTPException(status_code=503, detail="completion unavailable")
        payload = await request.json()
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        return await run_in_threadpool(runtime.complete_task, task_id)

    return app
