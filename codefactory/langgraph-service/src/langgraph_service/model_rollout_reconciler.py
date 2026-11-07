"""Reconcile durable workload lifecycle and model rollout events."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import time
from copy import deepcopy
from typing import Callable
from uuid import UUID

import httpx
from pydantic import TypeAdapter
import yaml

from langgraph_service.inference_config import InferenceInstance, InferenceInstanceConfig
from langgraph_service.model_download import validate_model_filename

NAMESPACE = "inference"
CONTAINER_NAME = "llama-server"
MODEL_PATH_PREFIX = "/models/"
OWNED_LABEL = "codefactory.io/managed-inference-workload"
TEMPLATE_DIRECTORY = Path("/etc/inference-workload-templates")
SERVICE_ACCOUNT_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IDEMPOTENCY_KEY = re.compile(
    r"(?:gpu|cpu)(?:-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)?:[1-9][0-9]*:[0-9a-f-]{36}\Z"
)
INFERENCE_INSTANCE_ADAPTER = TypeAdapter(InferenceInstance)
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
        request_id = str(UUID(str(event["rollout_request_id"])))
        instance_id = INFERENCE_INSTANCE_ADAPTER.validate_python(event["instance_id"])
        config_version = event["config_version"]
        model_filename = validate_model_filename(event["model_filename"])
        idempotency_key = event["idempotency_key"]
    except (KeyError, TypeError, ValueError) as error:
        raise ReconcileError("invalid rollout event") from error
    if (
        isinstance(config_version, bool)
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


def _set_integer_argument(args: list[str], option: str, value: int) -> list[str]:
    indexes = [index for index, item in enumerate(args) if item == option]
    if len(indexes) > 1 or (indexes and indexes[0] + 1 == len(args)):
        raise ReconcileError("deployment runtime arguments are invalid")
    result = list(args)
    if indexes:
        result[indexes[0] + 1] = str(value)
    else:
        result.extend((option, str(value)))
    return result


def _deployment_name(instance_id: str) -> str:
    if instance_id == "gpu":
        return "llama-cpp-server"
    if instance_id == "cpu":
        return "llama-cpp-server-cpu"
    return f"llama-cpp-server-{instance_id}"


class ModelRolloutReconciler:
    def __init__(
        self,
        *,
        service_client: httpx.Client,
        kubernetes_client: httpx.Client,
        poll_seconds: float = 5,
        rollout_timeout_seconds: float = 600,
        lease_seconds: int = 900,
        template_directory: Path = TEMPLATE_DIRECTORY,
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
        self._template_directory = template_directory
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

    def claim_workload(self) -> dict[str, object] | None:
        response = self._service.post(
            "/internal/reconciler/inference-workloads/claim",
            json={"lease_seconds": self._lease_seconds},
        )
        response.raise_for_status()
        body = response.json()
        event = body.get("event") if isinstance(body, dict) else None
        if event is not None and not isinstance(event, dict):
            raise ReconcileError("invalid workload claim response")
        return event

    def process_workload(self, event: dict[str, object]) -> str | None:
        try:
            event_id = str(UUID(str(event["event_id"])))
            lease_token = str(UUID(str(event["lease_token"])))
            instance_id = INFERENCE_INSTANCE_ADAPTER.validate_python(
                event["instance_id"]
            )
            operation = event["operation"]
            filename = validate_model_filename(event["model_filename"])
            if operation not in {"ensure", "delete"}:
                raise ValueError("invalid workload operation")
            config = InferenceInstanceConfig(
                instance_id=instance_id,
                model_filename=filename,
                context_size=event["context_size"],
                threads=event["threads"],
            )
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("workload event has invalid identifiers")
            return None

        succeeded = True
        try:
            if operation == "ensure":
                self._ensure_workload(
                    instance_id,
                    filename,
                    config.context_size,
                    config.threads,
                )
            else:
                self._delete_workload(instance_id)
        except Exception:
            succeeded = False
            LOGGER.warning("inference workload could not be reconciled")

        try:
            response = self._service.post(
                f"/internal/reconciler/inference-workloads/{event_id}/ack",
                json={
                    "lease_token": lease_token,
                    "outcome": "succeeded" if succeeded else "failed",
                },
            )
            response.raise_for_status()
            body = response.json()
            status = body.get("status") if isinstance(body, dict) else None
            if status not in {"succeeded", "retrying"}:
                raise ReconcileError("invalid workload acknowledgment response")
            return status
        except Exception:
            LOGGER.warning("inference workload acknowledgment could not be delivered")
            return None

    def _resource_url(self, group: str, resource: str, name: str | None = None) -> str:
        api_root = f"/apis/{group}/v1" if group else "/api/v1"
        base = f"{api_root}/namespaces/{NAMESPACE}/{resource}"
        return f"{base}/{name}" if name is not None else base

    def _load_template(
        self, instance_id: str, filename: str, context_size: int, threads: int
    ) -> dict[str, object]:
        profile = "gpu" if instance_id.startswith("gpu") else "cpu"
        path = self._template_directory / f"{profile}-deployment.yaml"
        try:
            template = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ReconcileError(
                "inference workload template is unavailable"
            ) from error
        if not isinstance(template, dict) or template.get("kind") != "Deployment":
            raise ReconcileError("inference workload template is invalid")
        deployment = deepcopy(template)
        name = _deployment_name(instance_id)
        labels = deployment.setdefault("metadata", {}).setdefault("labels", {})
        labels.update(
            {
                "app.kubernetes.io/name": name,
                "app.kubernetes.io/part-of": "inference-serving",
                OWNED_LABEL: "true",
                "codefactory.io/inference-instance": instance_id,
            }
        )
        deployment["metadata"]["name"] = name
        selector = (
            deployment["spec"].setdefault("selector", {}).setdefault("matchLabels", {})
        )
        selector["app.kubernetes.io/name"] = name
        pod_template = deployment["spec"].setdefault("template", {})
        pod_labels = pod_template.setdefault("metadata", {}).setdefault("labels", {})
        pod_labels.update(
            {
                "app.kubernetes.io/name": name,
                "app.kubernetes.io/part-of": "inference-serving",
                OWNED_LABEL: "true",
                "codefactory.io/inference-instance": instance_id,
            }
        )
        containers = pod_template.setdefault("spec", {}).get("containers", [])
        matches = [
            item
            for item in containers
            if isinstance(item, dict) and item.get("name") == CONTAINER_NAME
        ]
        if len(matches) != 1:
            raise ReconcileError("inference container is unavailable")
        args = _model_args(matches[0].get("args"), f"{MODEL_PATH_PREFIX}{filename}")
        args = _set_integer_argument(args, "--ctx-size", context_size)
        matches[0]["args"] = _set_integer_argument(args, "--threads", threads)
        return deployment

    def _get_resource(self, url: str) -> dict[str, object] | None:
        response = self._kubernetes.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ReconcileError("invalid Kubernetes resource response")
        return body

    def _ensure_service(self, instance_id: str, deployment: dict[str, object]) -> None:
        name = _deployment_name(instance_id)
        url = self._resource_url("", "services", name)
        existing = self._get_resource(url)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        ports = container.get("ports", [])
        port = next(
            (
                item
                for item in ports
                if isinstance(item, dict) and item.get("name") == "http"
            ),
            None,
        )
        if not isinstance(port, dict) or not isinstance(port.get("containerPort"), int):
            raise ReconcileError("inference service port is unavailable")
        desired = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/name": name,
                    "app.kubernetes.io/part-of": "inference-serving",
                    OWNED_LABEL: "true",
                    "codefactory.io/inference-instance": instance_id,
                },
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"app.kubernetes.io/name": name},
                "ports": [
                    {
                        "name": "http",
                        "port": port["containerPort"],
                        "targetPort": "http",
                        "protocol": "TCP",
                    }
                ],
            },
        }
        if existing is None:
            response = self._kubernetes.post(
                self._resource_url("", "services"),
                json=desired,
            )
            if response.status_code == 409:
                existing = self._get_resource(url)
            else:
                response.raise_for_status()
                return
        if existing is not None:
            self._require_owned_or_default(instance_id, existing)
            self._kubernetes.patch(
                url,
                json={
                    "metadata": {"labels": desired["metadata"]["labels"]},
                    "spec": desired["spec"],
                },
                headers={"Content-Type": "application/merge-patch+json"},
            ).raise_for_status()
        else:
            raise ReconcileError("inference Service could not be created")

    def _require_owned_or_default(
        self, instance_id: str, resource: dict[str, object]
    ) -> None:
        metadata = resource.get("metadata", {})
        labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        if isinstance(labels, dict) and labels.get(OWNED_LABEL) == "true":
            if labels.get("codefactory.io/inference-instance") != instance_id:
                raise ReconcileError("inference resource identity does not match")
            return
        if instance_id not in {"gpu", "cpu"} or not (
            isinstance(labels, dict)
            and labels.get("app.kubernetes.io/part-of") == "inference-serving"
        ):
            raise ReconcileError("inference resource is not controller-owned")

    def _ensure_workload(
        self, instance_id: str, filename: str, context_size: int, threads: int
    ) -> None:
        deployment = self._load_template(instance_id, filename, context_size, threads)
        name = _deployment_name(instance_id)
        url = self._resource_url("apps", "deployments", name)
        existing = self._get_resource(url)
        if existing is None:
            response = self._kubernetes.post(
                self._resource_url("apps", "deployments"), json=deployment
            )
            if response.status_code == 409:
                existing = self._get_resource(url)
            else:
                response.raise_for_status()
                existing = response.json()
        if existing is None:
            raise ReconcileError("inference Deployment could not be created")
        self._require_owned_or_default(instance_id, existing)
        self._ensure_service(instance_id, deployment)
        template = deployment["spec"]["template"]
        metadata = existing.get("metadata", {})
        generation = metadata.get("generation") if isinstance(metadata, dict) else None
        if not isinstance(generation, int) or generation < 1:
            raise ReconcileError("Deployment generation is unavailable")
        current_template = existing.get("spec", {}).get("template", {})
        current_spec = current_template.get("spec", {})
        current_containers = current_spec.get("containers", [])
        current_matches = [
            item
            for item in current_containers
            if isinstance(item, dict) and item.get("name") == CONTAINER_NAME
        ]
        desired_args = template["spec"]["containers"][0]["args"]
        current_args = (
            current_matches[0].get("args") if len(current_matches) == 1 else None
        )
        current_labels = existing.get("metadata", {}).get("labels", {})
        current_pod_labels = current_template.get("metadata", {}).get("labels", {})
        desired_labels = deployment["metadata"]["labels"]
        desired_pod_labels = template["metadata"]["labels"]
        ownership_needs_patch = any(
            not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in desired.items())
            for labels, desired in (
                (current_labels, desired_labels),
                (current_pod_labels, desired_pod_labels),
            )
        )
        patch = {
            "metadata": {"labels": desired_labels},
            "spec": {
                "template": {
                    "metadata": {"labels": desired_pod_labels},
                    "spec": {
                        "containers": [{"name": CONTAINER_NAME, "args": desired_args}]
                    },
                }
            },
        }
        target_generation = generation
        if current_args != desired_args or ownership_needs_patch:
            response = self._kubernetes.patch(
                url,
                json=patch,
                headers={"Content-Type": "application/strategic-merge-patch+json"},
            )
            response.raise_for_status()
            updated = response.json()
            target_generation = updated.get("metadata", {}).get("generation")
            if not isinstance(target_generation, int) or target_generation < generation:
                raise ReconcileError("Deployment update did not return a generation")
        replicas = existing.get("spec", {}).get("replicas", 1)
        if not isinstance(replicas, int) or replicas < 1:
            raise ReconcileError("Deployment has no desired replicas")
        deadline = self._monotonic() + self._rollout_timeout
        while True:
            current = self._get_resource(url)
            if current is None:
                raise ReconcileError("inference Deployment disappeared")
            status = current.get("status", {})
            observed = status.get("observedGeneration", 0)
            updated = status.get("updatedReplicas", 0)
            available = status.get("availableReplicas", 0)
            if (
                observed >= target_generation
                and status.get("replicas", 0) == replicas
                and updated >= replicas
                and available >= replicas
            ):
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ReconcileError("inference Deployment did not become ready")
            self._sleep(min(self._poll_seconds, remaining))

    def _delete_workload(self, instance_id: str) -> None:
        name = _deployment_name(instance_id)
        service_url = self._resource_url("", "services", name)
        for url in (service_url, self._resource_url("apps", "deployments", name)):
            resource = self._get_resource(url)
            if resource is None:
                continue
            self._require_owned_or_default(instance_id, resource)
            response = self._kubernetes.delete(url)
            if response.status_code != 404:
                response.raise_for_status()
            deadline = self._monotonic() + self._rollout_timeout
            while self._get_resource(url) is not None:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise ReconcileError("inference workload deletion timed out")
                self._sleep(min(self._poll_seconds, remaining))

    def _deployment_url(self, instance_id: str) -> str:
        deployment = (
            "llama-cpp-server"
            if instance_id == "gpu"
            else "llama-cpp-server-cpu"
            if instance_id == "cpu"
            else f"llama-cpp-server-{instance_id}"
        )
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
                and status.get("replicas", 0) == replicas
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
    template_directory = Path(
        os.environ.get("INFERENCE_WORKLOAD_TEMPLATE_DIRECTORY", str(TEMPLATE_DIRECTORY))
    )
    return ModelRolloutReconciler(
        service_client=service_client,
        kubernetes_client=kubernetes_client,
        poll_seconds=poll_seconds,
        rollout_timeout_seconds=rollout_timeout,
        lease_seconds=lease_seconds,
        template_directory=template_directory,
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
            workload_event = reconciler.claim_workload()
        except Exception:
            LOGGER.warning("inference workload claim could not be completed")
            time.sleep(reconciler._poll_seconds)
            continue
        if workload_event is not None:
            reconciler.process_workload(workload_event)
            continue
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
