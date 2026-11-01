"""Authenticated, framework-neutral GitHub webhook intake."""

from __future__ import annotations

import hashlib
import hmac
import json
import re

from github_service.events import GitHubWebhook, normalize_installation_id


class WebhookAuthenticationError(ValueError):
    """Raised when a webhook signature is missing or invalid."""


def decode_github_webhook(
    raw_body: bytes,
    *,
    event_name: str,
    delivery_id: str,
    signature: str | None,
    secret: str,
) -> GitHubWebhook:
    """Verify GitHub's SHA-256 signature before parsing the event payload."""
    if not secret or not isinstance(signature, str):
        raise WebhookAuthenticationError("invalid webhook signature")

    match = re.fullmatch(r"sha256=([0-9a-f]{64})", signature)
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if match is None or not hmac.compare_digest(match.group(1), expected):
        raise WebhookAuthenticationError("invalid webhook signature")

    if not event_name.strip() or not delivery_id.strip():
        raise ValueError("GitHub event name and delivery ID are required")

    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("webhook body must be valid JSON") from error
    if not isinstance(payload, dict):
        raise TypeError("webhook body must be a JSON object")
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise ValueError("GitHub webhook is missing installation.id")

    return GitHubWebhook(
        event_name=event_name,
        delivery_id=delivery_id,
        installation_id=normalize_installation_id(installation.get("id")),
        payload=payload,
    )
