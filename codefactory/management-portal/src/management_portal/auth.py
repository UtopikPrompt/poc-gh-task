"""OAuth and signed-session behavior for the management portal."""

from __future__ import annotations

import base64
from contextvars import ContextVar
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from management_portal.settings import PortalSettings

GITHUB_ORGANIZATION = "UtopikPrompt"
GITHUB_OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
DASHBOARD_SESSION_COOKIE = "codefactory_dashboard_session"
DASHBOARD_STATE_COOKIE = "codefactory_dashboard_oauth_state"
DASHBOARD_SESSION_SECONDS = 3600
DASHBOARD_STATE_SECONDS = 600
GITHUB_API_VERSION = "2022-11-28"
SAFE_AUTH_CONFIGURATION_ERROR = "Dashboard authentication is not configured"
OAUTH_FAILURE_REASON_CODES = frozenset(
    {
        "not_applicable",
        "organization_role_shape_invalid",
    }
)
OAUTH_CALLBACK_QUERY: ContextVar[dict[str, list[str]] | None] = ContextVar(
    "oauth_callback_query", default=None
)
logger = logging.getLogger(__name__)


def oauth_state_cookie_name(state: str) -> str:
    state_digest = hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]
    return f"{DASHBOARD_STATE_COOKIE}_{state_digest}"


class OrganizationMembershipDenied(Exception):
    def __init__(self, upstream_status: int | None = None):
        super().__init__()
        self.upstream_status = upstream_status


class OrganizationRoleLookupError(ValueError):
    def __init__(self, reason_code: str):
        if reason_code not in OAUTH_FAILURE_REASON_CODES:
            raise ValueError("Invalid OAuth diagnostic reason code")
        super().__init__()
        self.reason_code = reason_code


def _log_oauth_phase_failure(
    phase: str,
    error: Exception,
    upstream_status: int | None,
) -> None:
    if upstream_status is None and isinstance(error, httpx.HTTPStatusError):
        upstream_status = error.response.status_code
    reason_code = getattr(error, "reason_code", "not_applicable")
    if reason_code not in OAUTH_FAILURE_REASON_CODES:
        reason_code = "not_applicable"
    logger.warning(
        "OAuth callback phase failed phase=%s exception_class=%s "
        "upstream_status=%s reason_code=%s",
        phase,
        type(error).__name__,
        upstream_status if upstream_status is not None else "unavailable",
        reason_code,
    )


def install_oauth_callback_query_redaction(app: FastAPI) -> None:
    @app.middleware("http")
    async def redact_oauth_callback_query(request: Request, call_next):
        query_token = None
        if request.url.path == "/auth":
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


def require_dashboard_auth_configuration(settings: PortalSettings) -> None:
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


def sign_dashboard_session(login: str, signing_key: str, *, is_owner: bool) -> str:
    payload = json.dumps(
        {
            "login": login,
            "is_owner": is_owner,
            "expires_at": int(time.time()) + DASHBOARD_SESSION_SECONDS,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        signing_key.encode("utf-8"), encoded_payload.encode("ascii"), "sha256"
    ).hexdigest()
    return f"{encoded_payload}.{signature}"


def dashboard_session_login(
    request: Request,
    settings: PortalSettings,
) -> str | None:
    claims = _dashboard_session_claims(request, settings)
    login = claims.get("login") if claims else None
    return login if isinstance(login, str) else None


def dashboard_session_is_owner(
    request: Request,
    settings: PortalSettings,
) -> bool:
    claims = _dashboard_session_claims(request, settings)
    return claims is not None and claims.get("is_owner") is True


def _dashboard_session_claims(
    request: Request,
    settings: PortalSettings,
) -> dict[str, object] | None:
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
        is_owner = session.get("is_owner")
        if not isinstance(is_owner, bool):
            return None
        return {"login": login, "is_owner": is_owner}
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def clear_oauth_state_cookie(
    response: PlainTextResponse | RedirectResponse,
    state: str,
) -> None:
    response.delete_cookie(
        oauth_state_cookie_name(state),
        path="/auth",
        secure=True,
        httponly=True,
        samesite="lax",
    )


async def github_organization_member_login(
    request: Request,
    *,
    settings: PortalSettings,
    code: str,
) -> tuple[str, bool]:
    transport = getattr(request.app.state, "oauth_http_transport", None)
    phase = "token_exchange"
    upstream_status = None

    def record_upstream_status(status_code: int) -> None:
        nonlocal upstream_status
        upstream_status = status_code

    try:
        async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
            token_response = await client.post(
                GITHUB_OAUTH_TOKEN_URL,
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": settings.github_oauth_callback_url,
                },
                headers={"Accept": "application/json"},
            )
            upstream_status = token_response.status_code
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
            phase = "organization_membership_check"
            upstream_status = None
            membership_response = await client.get(
                f"{api_url}/user/memberships/orgs/{GITHUB_ORGANIZATION}",
                headers=headers,
            )
            upstream_status = membership_response.status_code
            if membership_response.status_code in {403, 404}:
                raise OrganizationMembershipDenied(upstream_status)
            membership_response.raise_for_status()
            membership = membership_response.json()
            if not isinstance(membership, dict):
                raise OrganizationRoleLookupError("organization_role_shape_invalid")
            organization = membership.get("organization", {})
            if (
                membership.get("state") != "active"
                or not isinstance(organization, dict)
                or organization.get("login", "").casefold()
                != GITHUB_ORGANIZATION.casefold()
            ):
                raise OrganizationMembershipDenied(upstream_status)
            role = membership.get("role")
            if not isinstance(role, str) or role not in {
                "admin",
                "member",
                "billing_manager",
            }:
                raise OrganizationRoleLookupError("organization_role_shape_invalid")
            is_owner = role == "admin"

            phase = "user_lookup"
            upstream_status = None
            user_response = await client.get(f"{api_url}/user", headers=headers)
            upstream_status = user_response.status_code
            user_response.raise_for_status()
            user = user_response.json()
            login = user.get("login")
            if not isinstance(login, str) or not login:
                raise ValueError("GitHub account unavailable")

            return login, is_owner
    except Exception as error:
        _log_oauth_phase_failure(phase, error, upstream_status)
        raise
