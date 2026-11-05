"""Reconcile durable model rollout events onto the two inference Deployments."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import time
from typing import Callable
from uuid import UUID

import httpx

from langgraph_service.model_download import validate_model_filename

NAMESPACE = "inference"
DEPLOYMENTS = {
    "gpu": "llama-cpp-server",
    "cpu": "llama-cpp-server-cpu",
}
CONTAINER_NAME = "llama-server"
MODEL_PATH_PREFIX = "/models/"
SERVICE_ACCOUNT_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IDEMPOTENCY_KEY = re.compile(r"(?:gpu|cpu):[1-9][0-9]*:[0-9a-f-]{36}\Z")
LOGGER = logging.getLogger(__name__)


class ReconcileError(Exception):
    """A rollout error that is safe to handle without exposing request details."""


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def _positive_integer(name: str, default: str, *, maximum: int | None = None) -> int:
    value = _required_environment(name) if name in os.environ else default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"{name} is outside its supported range")
    return parsed


def _event_values(event: object) -> tuple[str, str, str, str, int, str]:
    if not isinstance(event, dict):
        raise ReconcileError("invalid rollout event")
    try:
        event_id = str(UUID(str(event["event_id"])))
        lease_token = str(UUID(str(event["lease_token"])))
        request_id = str(UUID(str(event["download_request_id"])))
        instance_id = event["instance_id"]
        config_version = event["config_version"]
        model_filename = validate_model_filename(event["model_filename"])
        idempotency_key = event["idempotency_key"]
    except (KeyError, TypeError, ValueError) as error:
        raise ReconcileError("invalid rollout event") from error
    if (
        instance_id not in DEPLOYMENTS
        or isinstance(config_version, bool)
        or not isinstance(config_version, int)
        or config_version < 1
    ):
        raise ReconcileError("invalid rollout event")
    expected_key = f"{instance_id}:{config_version}:{request_id}"
    if (
        not isinstance(idempotency_key, str)
        or not IDEMPOTENCY_KEY.fullmatch(idempotency_key)
        or idempotency_key != expected_key
    ):
        raise ReconcileError("invalid rollout event")
    return (
        event_id,
        lease_token,
        instance_id,
        model_filename,
        config_version,
        idempotency_key,
    )


def _model_args(args: object, model_path: str) -> list[str]:
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ReconcileError("deployment model arguments are invalid")
    indexes = [index for index, value in enumerate(args) if value in {"-m", "--model"}]
    if len(indexes) != 1 or indexes[0] + 1 == len(args):
        raise ReconcileError("deployment model argument is missing or ambiguous")
    result = list(args)
    result[indexes[0]] = "--model"
    result[indexes[0] + 1] = model_path
    return result


class ModelRolloutReconciler:
    def __init__(
        self,
        *,
        service_client: httpx.Client,
        kubernetes_client: httpx.Client,
        poll_seconds: float = 5,
        rollout_timeout_seconds: float = 600,
        lease_seconds: int = 900,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if poll_seconds <= 0 or rollout_timeout_seconds <= 0:
            raise ValueError("poll and rollout timeout values must be positive")
        if not 30 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")
        if rollout_timeout_seconds >= lease_seconds:
            raise ValueError("rollout timeout must be shorter than the claim lease")
        self._service = service_client
        self._kubernetes = kubernetes_client
        self._poll_seconds = poll_seconds
        self._rollout_timeout = rollout_timeout_seconds
        self._lease_seconds = lease_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    def claim(self) -> dict[str, object] | None:
        response = self._service.post(
            "/internal/reconciler/inference-model-rollouts/claim",
            json={"lease_seconds": self._lease_seconds},
        )
        response.raise_for_status()
        body = response.json()
        event = body.get("event") if isinstance(body, dict) else None
        if event is not None and not isinstance(event, dict):
            raise ReconcileError("invalid rollout claim response")
        return event

    def process(self, event: dict[str, object]) -> str | None:
        try:
            event_id = str(UUID(str(event["event_id"])))
            lease_token = str(UUID(str(event["lease_token"])))
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("rollout event has invalid acknowledgment identifiers")
            return None

        outcome = "succeeded"
        try:
            values = _event_values(event)
            self._apply_and_wait(values)
        except Exception:
            outcome = "failed"
            LOGGER.warning("model rollout could not be applied")

        try:
            response = self._service.post(
                f"/internal/reconciler/inference-model-rollouts/{event_id}/ack",
                json={"lease_token": lease_token, "outcome": outcome},
            )
            response.raise_for_status()
            body = response.json()
            status = body.get("status") if isinstance(body, dict) else None
            if status not in {"succeeded", "failed", "superseded"}:
                raise ReconcileError("invalid rollout acknowledgment response")
            return status
        except Exception:
            LOGGER.warning("model rollout acknowledgment could not be delivered")
            return None

    def _deployment_url(self, instance_id: str) -> str:
        deployment = DEPLOYMENTS[instance_id]
        return f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{deployment}"

    def _get_deployment(self, instance_id: str) -> dict[str, object]:
        response = self._kubernetes.get(self._deployment_url(instance_id))
        response.raise_for_status()
        deployment = response.json()
        if not isinstance(deployment, dict):
            raise ReconcileError("invalid Deployment response")
        return deployment

    def _apply_and_wait(self, values: tuple[str, str, str, str, int, str]) -> None:
        _, _, instance_id, filename, _, idempotency_key = values
        model_path = f"{MODEL_PATH_PREFIX}{filename}"
        deployment = self._get_deployment(instance_id)
        generation = deployment.get("metadata", {}).get("generation")
        if not isinstance(generation, int) or generation < 1:
            raise ReconcileError("Deployment generation is unavailable")
        template = deployment.get("spec", {}).get("template", {})
        pod_spec = template.get("spec", {})
        containers = pod_spec.get("containers", [])
        matching = [
            container
            for container in containers
            if isinstance(container, dict) and container.get("name") == CONTAINER_NAME
        ]
        if len(matching) != 1:
            raise ReconcileError("inference container is unavailable")
        args = _model_args(matching[0].get("args"), model_path)
        annotations = template.get("metadata", {}).get("annotations", {})
        if not isinstance(annotations, dict):
            raise ReconcileError("Deployment annotations are invalid")

        if (
            args == matching[0].get("args")
            and annotations.get("codefactory.io/model-rollout-idempotency-key")
            == idempotency_key
        ):
            target_generation = generation
        else:
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "codefactory.io/model-rollout-idempotency-key": idempotency_key
                            }
                        },
                        "spec": {
                            "containers": [{"name": CONTAINER_NAME, "args": args}]
                        },
                    }
                }
            }
            response = self._kubernetes.patch(
                self._deployment_url(instance_id),
                json=patch,
                headers={"Content-Type": "application/strategic-merge-patch+json"},
            )
            response.raise_for_status()
            patched = response.json()
            target_generation = patched.get("metadata", {}).get("generation")
            if not isinstance(target_generation, int) or target_generation < generation:
                raise ReconcileError("Deployment patch did not return a generation")

        replicas = deployment.get("spec", {}).get("replicas", 1)
        if not isinstance(replicas, int) or replicas < 1:
            raise ReconcileError("Deployment has no desired replicas")
        deadline = self._monotonic() + self._rollout_timeout
        while True:
            current = self._get_deployment(instance_id)
            current_template = current.get("spec", {}).get("template", {})
            current_spec = current_template.get("spec", {})
            current_containers = current_spec.get("containers", [])
            current_matches = [
                container
                for container in current_containers
                if isinstance(container, dict)
                and container.get("name") == CONTAINER_NAME
            ]
            current_annotations = current_template.get("metadata", {}).get(
                "annotations", {}
            )
            if (
                len(current_matches) != 1
                or current_matches[0].get("args") != args
                or not isinstance(current_annotations, dict)
                or current_annotations.get(
                    "codefactory.io/model-rollout-idempotency-key"
                )
                != idempotency_key
            ):
                raise ReconcileError("Deployment template changed during rollout")
            current_generation = current.get("metadata", {}).get("generation", 0)
            status = current.get("status", {})
            if (
                isinstance(current_generation, int)
                and current_generation >= target_generation
                and status.get("observedGeneration", 0) >= target_generation
                and status.get("updatedReplicas", 0) >= replicas
                and status.get("availableReplicas", 0) >= replicas
            ):
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ReconcileError("Deployment rollout timed out")
            self._sleep(min(self._poll_seconds, remaining))


def _make_reconciler() -> ModelRolloutReconciler:
    service_url = _required_environment("LANGGRAPH_SERVICE_URL").rstrip("/")
    token = _required_environment("MODEL_RECONCILER_TOKEN")
    service_client = httpx.Client(
        base_url=service_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        trust_env=False,
    )
    host = _required_environment("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("KUBERNETES_SERVICE_PORT is invalid")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    account_directory = SERVICE_ACCOUNT_DIRECTORY
    ca_file = account_directory / "ca.crt"
    api_token = (account_directory / "token").read_text(encoding="utf-8").strip()
    if not api_token or not ca_file.is_file():
        raise ValueError("Kubernetes service account credentials are unavailable")
    kubernetes_client = httpx.Client(
        base_url=f"https://{host}:{port}",
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=10,
        verify=str(ca_file),
        trust_env=False,
    )
    lease_seconds = _positive_integer("MODEL_ROLLOUT_LEASE_SECONDS", "900", maximum=900)
    if lease_seconds < 30:
        raise ValueError("MODEL_ROLLOUT_LEASE_SECONDS must be at least 30")
    poll_seconds = float(os.environ.get("MODEL_ROLLOUT_POLL_SECONDS", "5"))
    rollout_timeout = float(os.environ.get("MODEL_ROLLOUT_TIMEOUT_SECONDS", "600"))
    return ModelRolloutReconciler(
        service_client=service_client,
        kubernetes_client=kubernetes_client,
        poll_seconds=poll_seconds,
        rollout_timeout_seconds=rollout_timeout,
        lease_seconds=lease_seconds,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        reconciler = _make_reconciler()
    except (OSError, ValueError):
        LOGGER.error("model reconciler configuration is unavailable")
        raise SystemExit(1)
    while True:
        try:
            event = reconciler.claim()
        except Exception:
            LOGGER.warning("model rollout claim could not be completed")
            time.sleep(reconciler._poll_seconds)
            continue
        if event is None:
            time.sleep(reconciler._poll_seconds)
            continue
        reconciler.process(event)


if __name__ == "__main__":
    main()
