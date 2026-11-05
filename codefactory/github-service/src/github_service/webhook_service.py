"""Application service for applying normalized GitHub deliveries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from github_service.events import (
    GitHubWebhook,
    _log_webhook_decision,
    normalize_installation_id,
)


class DeliveryStore(Protocol):
    def task_for_pull_request(
        self,
        repository: str,
        pull_request_number: int,
    ) -> str | None: ...

    def claim_delivery(self, delivery_id: str) -> str: ...

    def complete_delivery(
        self,
        delivery_id: str,
        response: dict[str, object],
    ) -> None: ...

    def fail_delivery(self, delivery_id: str) -> None: ...

    def enqueue_events(
        self,
        delivery_id: str,
        events: Sequence[Mapping[str, object]],
    ) -> list[str]: ...


class EventNormalizer(Protocol):
    def __call__(
        self,
        webhook: GitHubWebhook,
        store: DeliveryStore,
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]] | None: ...


class WebhookService:
    def __init__(
        self,
        *,
        store: DeliveryStore,
        normalize_event: EventNormalizer,
    ):
        self._store = store
        self._normalize_event = normalize_event

    def handle(self, webhook: GitHubWebhook) -> dict[str, object]:
        delivery_id = webhook.delivery_id
        claim = self._store.claim_delivery(delivery_id)
        if claim == "duplicate":
            _log_webhook_decision(webhook, "duplicate")
            return {"status": "duplicate", "delivery_id": delivery_id}
        if claim == "in_progress":
            _log_webhook_decision(webhook, "duplicate")
            return {"status": "in_progress", "delivery_id": delivery_id}

        failure_reason = "processing_failure"
        try:
            webhook_installation_id = normalize_installation_id(webhook.installation_id)
            failure_reason = "resolver_or_repository_validation_failure"
            normalized = self._normalize_event(webhook, self._store)
            if normalized is None:
                response: dict[str, object] = {
                    "status": "ignored",
                    "delivery_id": delivery_id,
                }
            else:
                events = (
                    [normalized]
                    if isinstance(normalized, Mapping)
                    else list(normalized)
                    if isinstance(normalized, Sequence)
                    else None
                )
                if events is None or any(
                    not isinstance(event, Mapping) for event in events
                ):
                    failure_reason = "normalized_event_validation_failure"
                    raise TypeError("event normalizer must return mapping events")
                normalized_events: list[Mapping[str, object]] = []
                for event in events:
                    task_id_value = event.get("task_id")
                    event_type = event.get("event_type")
                    if not isinstance(task_id_value, str) or not task_id_value.strip():
                        failure_reason = "normalized_event_validation_failure"
                        raise ValueError("normalized event must include a task_id")
                    if event_type not in {
                        "ready",
                        "ci",
                        "merged",
                        "review_changes_requested",
                        "human_feedback_reply",
                        "agent_repair_requested",
                    }:
                        failure_reason = "normalized_event_validation_failure"
                        raise ValueError(
                            "normalized event has an unsupported event_type"
                        )
                    normalized_event = dict(event)
                    event_installation_id = normalize_installation_id(
                        normalized_event.get("installation_id")
                    )
                    if event_installation_id != webhook_installation_id:
                        failure_reason = "normalized_event_validation_failure"
                        raise ValueError(
                            "normalized event installation does not match webhook"
                        )
                    normalized_event["installation_id"] = event_installation_id
                    normalized_event["webhook_delivery_id"] = delivery_id
                    normalized_events.append(normalized_event)
                if not normalized_events:
                    response = {"status": "ignored", "delivery_id": delivery_id}
                else:
                    event_ids = self._store.enqueue_events(
                        delivery_id,
                        normalized_events,
                    )
                    response = {
                        "status": "queued",
                        "delivery_id": delivery_id,
                        "event_count": len(event_ids),
                        "event_ids": event_ids,
                    }
            self._store.complete_delivery(delivery_id, response)
            if response["status"] == "queued":
                _log_webhook_decision(webhook, "accepted")
            return response
        except Exception:
            _log_webhook_decision(webhook, failure_reason)
            self._store.fail_delivery(delivery_id)
            raise
