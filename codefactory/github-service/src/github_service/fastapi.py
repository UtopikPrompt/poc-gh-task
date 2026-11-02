"""FastAPI service for authenticated, checkpointed GitHub webhook intake."""

from __future__ import annotations

import base64
from contextvars import ContextVar
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from github_service.settings import RuntimeSettings
from github_service.publishing import FileChangeProposal
from github_service.webhook_service import WebhookService
from github_service.webhooks import (
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
    propose_file_changes: Callable[[FileChangeProposal], dict[str, object]] | None = (
        None
    )
    get_source_tree: Callable[[str], dict[str, object]] | None = None
    get_source_files: Callable[[str, object], dict[str, object]] | None = None


MAX_PROPOSAL_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SOURCE_REQUEST_BYTES = 8 * 1024
GITHUB_ORGANIZATION = "UtopikPrompt"
GITHUB_OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_OAUTH_CALLBACK_URL = "https://codefactory.utopiklab.com/auth/callback"
DASHBOARD_SESSION_COOKIE = "codefactory_dashboard_session"
DASHBOARD_STATE_COOKIE = "codefactory_dashboard_oauth_state"
DASHBOARD_SESSION_SECONDS = 3600
DASHBOARD_STATE_SECONDS = 600
GITHUB_API_VERSION = "2022-11-28"
SAFE_AUTH_CONFIGURATION_ERROR = "Dashboard authentication is not configured"
OAUTH_CALLBACK_QUERY: ContextVar[dict[str, list[str]] | None] = ContextVar(
    "oauth_callback_query", default=None
)
WORKFLOW_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Codefactory workflow state</title>
    <style>
        :root { color-scheme: light; font-family: ui-sans-serif, system-ui, sans-serif;
            background: #f3f6f5; color: #172321; }
        body { margin: 0; }
        main { max-width: 1180px; margin: 0 auto; padding: 32px 24px; }
        header { display: flex; align-items: center; justify-content: space-between;
            gap: 16px; border-bottom: 1px solid #cbd6d2; padding-bottom: 18px; }
        header form { margin: 0; }
        h1 { font-size: 24px; margin: 0; }
        button { border: 1px solid #45655d; background: #fff; color: #173c34;
            padding: 8px 12px; font: inherit; cursor: pointer; }
        button:hover { background: #e6efec; }
        #message { color: #53625e; min-height: 24px; margin: 18px 0 8px; }
        .table-wrap { overflow-x: auto; border-top: 2px solid #256653; }
        table { border-collapse: collapse; width: 100%; min-width: 850px; text-align: left; }
        th { color: #53625e; font-size: 12px; font-weight: 600; }
        th, td { padding: 12px 10px; border-bottom: 1px solid #d5dedb; }
        td { font-size: 14px; vertical-align: top; }
        code { overflow-wrap: anywhere; }
        .status { font-weight: 650; color: #175947; }
        .failed { color: #a13d32; }
        @media (max-width: 600px) { main { padding: 22px 14px; }
            header { align-items: flex-start; } }
    </style>
</head>
<body>
    <main>
        <header><h1>Codefactory workflow state</h1>
            <form action="/auth/logout" method="post"><button type="submit">Sign out</button></form>
            <button id="refresh" type="button">Refresh</button></header>
        <p id="message" role="status" aria-live="polite">Loading workflow state...</p>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Task</th><th>Repository</th><th>Issue</th>
                    <th>Pull request</th><th>Latest event</th><th>Status</th>
                    <th>Updated</th></tr></thead>
                <tbody id="workflows"></tbody>
            </table>
        </div>
    </main>
    <script>
        const message = document.getElementById('message');
        const body = document.getElementById('workflows');
        const cell = (row, value) => {
            const element = row.insertCell();
            element.textContent = value == null ? 'Unavailable' : String(value);
            return element;
        };
        async function refresh() {
            message.textContent = 'Loading workflow state...';
            body.replaceChildren();
            try {
                const response = await fetch('/api/workflows', { headers: { Accept: 'application/json' } });
                if (!response.ok) throw new Error('unavailable');
                const data = await response.json();
                for (const workflow of data.workflows) {
                    const row = body.insertRow();
                    cell(row, workflow.task_id);
                    cell(row, workflow.repository);
                    cell(row, workflow.issue_number == null ? null : `#${workflow.issue_number}`);
                    cell(row, workflow.pull_request_number == null ? null : `#${workflow.pull_request_number}`);
                    cell(row, workflow.latest_event);
                    const status = cell(row, workflow.has_failure ? `${workflow.status} (failure recorded)` : workflow.status);
                    status.className = `status${workflow.status === 'failed' ? ' failed' : ''}`;
                    cell(row, workflow.latest_event_at);
                }
                message.textContent = data.workflows.length ? `${data.workflows.length} workflow(s)` : 'No workflow records are available yet.';
            } catch {
                message.textContent = 'Workflow state is unavailable.';
            }
        }
        document.getElementById('refresh').addEventListener('click', refresh);
        refresh();
    </script>
</body>
</html>"""


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

    @app.middleware("http")
    async def redact_oauth_callback_query(request: Request, call_next):
        query_token = None
        if request.url.path == "/auth/callback":
            query_token = OAUTH_CALLBACK_QUERY.set(
                parse_qs(
                    request.scope.get("query_string", b"").decode("latin-1"),
                    keep_blank_values=True,
                )
            )
            request.scope["query_string"] = b""
        try:
            return await call_next(request)
        finally:
            if query_token is not None:
                OAUTH_CALLBACK_QUERY.reset(query_token)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/login")
    async def dashboard_login() -> RedirectResponse:
        _require_dashboard_auth_configuration(settings)
        state = secrets.token_urlsafe(32)
        location = f"{GITHUB_OAUTH_AUTHORIZE_URL}?{
            urlencode(
                {
                    'client_id': settings.github_oauth_client_id,
                    'redirect_uri': GITHUB_OAUTH_CALLBACK_URL,
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
            path="/auth/callback",
        )
        return response

    @app.get("/auth/callback")
    async def dashboard_oauth_callback(request: Request):
        _require_dashboard_auth_configuration(settings)
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
            _clear_oauth_state_cookie(response)
            return response

        code_values = query.get("code", [])
        if len(code_values) != 1 or not code_values[0]:
            raise HTTPException(status_code=400, detail="invalid OAuth response")

        try:
            login = await _github_organization_member_login(
                request,
                settings=settings,
                code=code_values[0],
            )
        except _OrganizationMembershipDenied:
            response = PlainTextResponse(
                "Active membership in the approved GitHub organization is required.",
                status_code=403,
            )
            _clear_oauth_state_cookie(response)
            return response
        except Exception:
            response = PlainTextResponse(
                "GitHub authorization is temporarily unavailable.", status_code=502
            )
            _clear_oauth_state_cookie(response)
            return response

        response = RedirectResponse("/", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            DASHBOARD_SESSION_COOKIE,
            _sign_dashboard_session(
                login,
                settings.dashboard_session_signing_key or "",
            ),
            max_age=DASHBOARD_SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        _clear_oauth_state_cookie(response)
        return response

    @app.post("/auth/logout")
    async def dashboard_logout() -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            DASHBOARD_SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        _clear_oauth_state_cookie(response)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def workflow_dashboard(request: Request):
        _require_dashboard_auth_configuration(settings)
        if _dashboard_session_login(request, settings) is None:
            return RedirectResponse("/auth/login", status_code=303)
        return HTMLResponse(WORKFLOW_DASHBOARD_HTML)

    @app.get("/api/workflows")
    async def workflow_state(request: Request) -> dict[str, object]:
        _require_dashboard_auth_configuration(settings)
        if _dashboard_session_login(request, settings) is None:
            raise HTTPException(status_code=401, detail="authentication required")
        runtime = getattr(request.app.state, "internal_runtime", None)
        store = getattr(runtime, "store", None)
        list_workflows = getattr(store, "list_workflows", None)
        if not callable(list_workflows):
            raise HTTPException(status_code=503, detail="workflow state unavailable")
        try:
            workflows = await run_in_threadpool(list_workflows)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="workflow state unavailable"
            ) from error
        return {"workflows": workflows}

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
        if not isinstance(payload, dict) or set(payload) != {"task_id", "paths"}:
            raise HTTPException(
                status_code=400, detail="request must contain task_id and paths"
            )
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        try:
            return await run_in_threadpool(
                runtime.get_source_files, task_id, payload.get("paths")
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
        return await run_in_threadpool(runtime.complete_task, task_id)

    return app


class _OrganizationMembershipDenied(Exception):
    pass


def _require_dashboard_auth_configuration(settings: RuntimeSettings) -> None:
    signing_key = settings.dashboard_session_signing_key
    if (
        not all(
            (
                settings.github_oauth_client_id,
                settings.github_oauth_client_secret,
                signing_key,
            )
        )
        or len(signing_key.encode("utf-8")) < 32
    ):
        raise HTTPException(status_code=503, detail=SAFE_AUTH_CONFIGURATION_ERROR)


def _sign_dashboard_session(login: str, signing_key: str) -> str:
    payload = json.dumps(
        {"login": login, "expires_at": int(time.time()) + DASHBOARD_SESSION_SECONDS},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        signing_key.encode("utf-8"), encoded_payload.encode("ascii"), "sha256"
    ).hexdigest()
    return f"{encoded_payload}.{signature}"


def _dashboard_session_login(
    request: Request,
    settings: RuntimeSettings,
) -> str | None:
    cookie = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    signing_key = settings.dashboard_session_signing_key
    if not cookie or not signing_key:
        return None
    try:
        encoded_payload, supplied_signature = cookie.split(".", 1)
        expected_signature = hmac.new(
            signing_key.encode("utf-8"), encoded_payload.encode("ascii"), "sha256"
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = base64.urlsafe_b64decode(
            encoded_payload + "=" * (-len(encoded_payload) % 4)
        )
        session = json.loads(payload)
        if not isinstance(session, dict):
            return None
        expires_at = session.get("expires_at")
        login = session.get("login")
        now = int(time.time())
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= now
            or expires_at > now + DASHBOARD_SESSION_SECONDS
            or not isinstance(login, str)
            or not login
        ):
            return None
        return login
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _clear_oauth_state_cookie(response: PlainTextResponse | RedirectResponse) -> None:
    response.delete_cookie(
        DASHBOARD_STATE_COOKIE,
        path="/auth/callback",
        secure=True,
        httponly=True,
        samesite="lax",
    )


async def _github_organization_member_login(
    request: Request,
    *,
    settings: RuntimeSettings,
    code: str,
) -> str:
    transport = getattr(request.app.state, "oauth_http_transport", None)
    async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
        token_response = await client.post(
            GITHUB_OAUTH_TOKEN_URL,
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
                "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
            },
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        scopes = token_data.get("scope", "")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(scopes, str)
            or "read:org" not in scopes.split(",")
        ):
            raise ValueError("required OAuth authorization unavailable")

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        api_url = settings.github_api_url.rstrip("/")
        membership_response = await client.get(
            f"{api_url}/user/memberships/orgs/{GITHUB_ORGANIZATION}",
            headers=headers,
        )
        if membership_response.status_code in {403, 404}:
            raise _OrganizationMembershipDenied
        membership_response.raise_for_status()
        membership = membership_response.json()
        organization = membership.get("organization", {})
        if (
            membership.get("state") != "active"
            or not isinstance(organization, dict)
            or organization.get("login", "").casefold()
            != GITHUB_ORGANIZATION.casefold()
        ):
            raise _OrganizationMembershipDenied

        user_response = await client.get(f"{api_url}/user", headers=headers)
        user_response.raise_for_status()
        user = user_response.json()
        login = user.get("login")
        if not isinstance(login, str) or not login:
            raise ValueError("GitHub account unavailable")
        return login


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
