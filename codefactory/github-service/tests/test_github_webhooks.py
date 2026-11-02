import hashlib
import hmac
import json

import pytest
from github_service.webhooks import (
    WebhookAuthenticationError,
    decode_github_webhook,
)


def _signature(body, secret="test-secret"):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _decode(
    body,
    signature,
    *,
    event_name="projects_v2_item",
    delivery_id="delivery-1",
):
    return decode_github_webhook(
        raw_body=body,
        event_name=event_name,
        delivery_id=delivery_id,
        signature=signature,
        secret="test-secret",
    )


def test_valid_delivery_decodes_authenticated_github_event():
    body = json.dumps({"action": "edited", "installation": {"id": 654}}).encode()
    webhook = _decode(body, _signature(body))

    assert webhook.event_name == "projects_v2_item"
    assert webhook.delivery_id == "delivery-1"
    assert webhook.installation_id == 654
    assert webhook.payload == {"action": "edited", "installation": {"id": 654}}


def test_invalid_signature_is_rejected_before_payload_parsing():
    with pytest.raises(WebhookAuthenticationError):
        _decode(b"not-json", "sha256=" + "0" * 64)


@pytest.mark.parametrize(
    ("body", "event_name", "delivery_id", "exception"),
    [
        (b"not-json", "projects_v2_item", "delivery-1", ValueError),
        (b"[]", "projects_v2_item", "delivery-1", TypeError),
        (b"{}", "", "delivery-1", ValueError),
        (b"{}", "projects_v2_item", "", ValueError),
        (
            b'{"installation": {"id": 0}}',
            "projects_v2_item",
            "delivery-1",
            ValueError,
        ),
        (
            b'{"installation": {"id": "654"}}',
            "projects_v2_item",
            "delivery-1",
            None,
        ),
        (
            b'{"installation": {"id": true}}',
            "projects_v2_item",
            "delivery-1",
            ValueError,
        ),
        (b"{}", "projects_v2_item", "delivery-1", ValueError),
    ],
)
def test_invalid_verified_webhook_is_rejected(body, event_name, delivery_id, exception):
    if exception is None:
        webhook = _decode(
            body,
            _signature(body),
            event_name=event_name,
            delivery_id=delivery_id,
        )
        assert webhook.installation_id == 654
        return
    with pytest.raises(exception):
        _decode(
            body,
            _signature(body),
            event_name=event_name,
            delivery_id=delivery_id,
        )
