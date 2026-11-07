import json
from uuid import uuid4

import httpx

from langgraph_service.model_rollout_reconciler import ModelRolloutReconciler


def _event(**overrides):
    request_id = str(uuid4())
    event = {
        "event_id": str(uuid4()),
        "instance_id": "gpu",
        "rollout_request_id": request_id,
        "model_filename": "approved.gguf",
        "config_version": 7,
        "idempotency_key": f"gpu:7:{request_id}",
        "lease_token": str(uuid4()),
    }
    event.update(overrides)
    return event


def _deployment(*, generation=2, observed=2, updated=1, available=1, args=None):
    return {
        "metadata": {"generation": generation},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"annotations": {}},
                "spec": {
                    "containers": [
                        {
                            "name": "llama-server",
                            "args": args or ["--verbose", "-m", "/models/old.gguf"],
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": observed,
            "replicas": 1,
            "updatedReplicas": updated,
            "availableReplicas": available,
        },
    }


def _reconciler(service_transport, kubernetes_transport, **kwargs):
    settings = {
        "poll_seconds": 0.001,
        "rollout_timeout_seconds": 1,
        "lease_seconds": 30,
    }
    settings.update(kwargs)
    return ModelRolloutReconciler(
        service_client=httpx.Client(
            base_url="http://langgraph", transport=service_transport
        ),
        kubernetes_client=httpx.Client(
            base_url="https://kubernetes", transport=kubernetes_transport
        ),
        **settings,
    )


def _workload_event(operation="ensure", instance_id="gpu-secondary"):
    return {
        "event_id": str(uuid4()),
        "instance_id": instance_id,
        "operation": operation,
        "model_filename": "approved.gguf",
        "context_size": 4096 if instance_id.startswith("cpu") else 8192,
        "threads": 3,
        "lease_token": str(uuid4()),
        "attempts": 1,
    }


def _write_templates(directory):
    directory.mkdir()
    template = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llama-cpp-server
  labels:
    app.kubernetes.io/name: llama-cpp-server
    app.kubernetes.io/part-of: inference-serving
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: llama-cpp-server
  template:
    metadata:
      labels:
        app.kubernetes.io/name: llama-cpp-server
        app.kubernetes.io/part-of: inference-serving
    spec:
      containers:
      - name: llama-server
        args: [--model, /models/model.gguf]
        ports:
        - name: http
          containerPort: 8080
"""
    for profile in ("gpu", "cpu"):
        (directory / f"{profile}-deployment.yaml").write_text(template)


def _owned_workload(instance_id="gpu-secondary"):
    name = f"llama-cpp-server-{instance_id}"
    labels = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": "inference-serving",
        "codefactory.io/managed-inference-workload": "true",
        "codefactory.io/inference-instance": instance_id,
    }
    return {
        "metadata": {"name": name, "labels": labels, "generation": 1},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "llama-server",
                            "args": ["--model", "/models/approved.gguf"],
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }


def test_reconciler_patches_only_the_mapped_deployment_and_acks_ready():
    event = _event()
    requests = []
    patched = None

    def kubernetes(request):
        nonlocal patched
        requests.append(request)
        if request.method == "PATCH":
            patched = request.read()
            return httpx.Response(200, json=_deployment(generation=3, observed=2))
        if patched is not None:
            current = _deployment(
                generation=3,
                observed=3,
                args=["--verbose", "--model", "/models/approved.gguf"],
            )
            current["spec"]["template"]["metadata"]["annotations"] = {
                "codefactory.io/model-rollout-idempotency-key": event["idempotency_key"]
            }
            return httpx.Response(
                200,
                json=current,
            )
        return httpx.Response(200, json=_deployment())

    acknowledgments = []

    def service(request):
        acknowledgments.append((request.url.path, request.read()))
        return httpx.Response(
            200,
            json={"event_id": event["event_id"], "status": "succeeded"},
        )

    reconciler = _reconciler(
        httpx.MockTransport(service), httpx.MockTransport(kubernetes)
    )

    assert reconciler.process(event) == "succeeded"
    assert all(request.url.path.endswith("/llama-cpp-server") for request in requests)
    assert len([request for request in requests if request.method == "PATCH"]) == 1
    assert b"/models/approved.gguf" in patched
    assert b"gpu:7:" in patched
    assert acknowledgments[0][0].endswith(f"/{event['event_id']}/ack")
    assert b'"outcome":"succeeded"' in acknowledgments[0][1]


def test_unsafe_filename_is_failed_without_kubernetes_access():
    event = _event(model_filename="../outside.gguf")
    acknowledgments = []
    service = httpx.MockTransport(
        lambda request: (
            acknowledgments.append(request.read())
            or httpx.Response(
                200,
                json={"event_id": event["event_id"], "status": "failed"},
            )
        )
    )
    kubernetes = httpx.MockTransport(
        lambda _request: (_ for _ in ()).throw(AssertionError("unexpected API access"))
    )

    result = _reconciler(service, kubernetes).process(event)

    assert result == "failed"
    assert b'"outcome":"failed"' in acknowledgments[0]


def test_reconciler_targets_secondary_gpu_deployment():
    event = _event(instance_id="gpu-secondary")
    event["idempotency_key"] = (
        f"gpu-secondary:{event['config_version']}:{event['rollout_request_id']}"
    )
    paths = []

    def kubernetes(request):
        paths.append(request.url.path)
        if request.method == "PATCH":
            return httpx.Response(200, json=_deployment(generation=3, observed=2))
        current = _deployment(
            generation=3,
            observed=3,
            args=["--verbose", "--model", "/models/approved.gguf"],
        )
        current["spec"]["template"]["metadata"]["annotations"] = {
            "codefactory.io/model-rollout-idempotency-key": event["idempotency_key"]
        }
        return httpx.Response(200, json=current)

    service = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"event_id": event["event_id"], "status": "succeeded"}
        )
    )

    result = _reconciler(service, httpx.MockTransport(kubernetes)).process(event)

    assert result == "succeeded"
    assert paths
    assert all(
        path.endswith("/deployments/llama-cpp-server-gpu-secondary") for path in paths
    )


def test_reconciler_fails_ack_when_deployment_does_not_converge():
    event = _event(instance_id="cpu", idempotency_key=None)
    event["idempotency_key"] = f"cpu:7:{event['rollout_request_id']}"
    acknowledgments = []
    deployment_paths = []

    def service(request):
        acknowledgments.append(request.read())
        return httpx.Response(
            200, json={"event_id": event["event_id"], "status": "failed"}
        )

    def kubernetes(request):
        deployment_paths.append(request.url.path)
        if request.method == "PATCH":
            return httpx.Response(200, json=_deployment(generation=3))
        return httpx.Response(
            200,
            json=_deployment(
                generation=3,
                observed=2,
                updated=0,
                available=0,
                args=["--verbose", "--model", "/models/approved.gguf"],
            ),
        )

    reconciler = _reconciler(
        httpx.MockTransport(service),
        httpx.MockTransport(kubernetes),
        rollout_timeout_seconds=0.01,
    )

    assert reconciler.process(event) == "failed"
    assert deployment_paths
    assert all(
        path == "/apis/apps/v1/namespaces/inference/deployments/llama-cpp-server-cpu"
        for path in deployment_paths
    )
    assert b'"outcome":"failed"' in acknowledgments[0]


def test_workload_create_waits_for_deployment_rollout_convergence(tmp_path):
    event = _workload_event()
    templates = tmp_path / "templates"
    _write_templates(templates)
    resources = {}
    requests = []
    deployment_gets = 0

    def kubernetes(request):
        nonlocal deployment_gets
        requests.append(request)
        path = request.url.path
        if request.method == "GET":
            resource = resources.get(path)
            if resource and resource.get("kind") == "Deployment":
                deployment_gets += 1
                resource["status"]["replicas"] = 2 if deployment_gets == 1 else 1
            return (
                httpx.Response(200, json=resource) if resource else httpx.Response(404)
            )
        if request.method == "POST":
            resource = json.loads(request.read())
            resource.setdefault("metadata", {})["generation"] = 1
            if resource["kind"] == "Deployment":
                resource["status"] = {
                    "observedGeneration": 1,
                    "replicas": 2,
                    "updatedReplicas": 1,
                    "availableReplicas": 1,
                }
            name = resource["metadata"]["name"]
            kind = "deployments" if resource["kind"] == "Deployment" else "services"
            group = "/apis/apps/v1" if kind == "deployments" else "/api/v1"
            resources[f"{group}/namespaces/inference/{kind}/{name}"] = resource
            return httpx.Response(201, json=resource)
        raise AssertionError(f"unexpected Kubernetes method: {request.method}")

    acknowledgments = []

    def service(request):
        acknowledgments.append((request.url.path, request.read()))
        return httpx.Response(
            200, json={"event_id": event["event_id"], "status": "succeeded"}
        )

    reconciler = _reconciler(
        httpx.MockTransport(service),
        httpx.MockTransport(kubernetes),
        template_directory=templates,
    )

    assert reconciler.process_workload(event) == "succeeded"
    assert deployment_gets == 2
    created = resources[
        "/apis/apps/v1/namespaces/inference/deployments/llama-cpp-server-gpu-secondary"
    ]
    assert (
        created["metadata"]["labels"]["codefactory.io/inference-instance"]
        == "gpu-secondary"
    )
    assert created["spec"]["template"]["spec"]["containers"][0]["args"] == [
        "--model",
        "/models/approved.gguf",
        "--ctx-size",
        "8192",
        "--threads",
        "3",
    ]
    service = resources[
        "/api/v1/namespaces/inference/services/llama-cpp-server-gpu-secondary"
    ]
    assert service["spec"]["selector"] == {
        "app.kubernetes.io/name": "llama-cpp-server-gpu-secondary"
    }
    assert service["spec"]["ports"][0]["port"] == 8080
    assert any(
        request.method == "POST" and "/services" in request.url.path
        for request in requests
    )
    assert acknowledgments[0][0].endswith(f"/{event['event_id']}/ack")
    assert b'"outcome":"succeeded"' in acknowledgments[0][1]


def test_workload_delete_removes_only_controller_owned_resources():
    event = _workload_event(operation="delete")
    deployment = _owned_workload()
    name = deployment["metadata"]["name"]
    deployment_url = f"/apis/apps/v1/namespaces/inference/deployments/{name}"
    service_url = f"/api/v1/namespaces/inference/services/{name}"
    service = {
        "metadata": {
            "name": name,
            "labels": deployment["metadata"]["labels"],
        }
    }
    resources = {deployment_url: deployment, service_url: service}
    deleted = []

    def kubernetes(request):
        path = request.url.path
        if request.method == "GET":
            return (
                httpx.Response(200, json=resources[path])
                if path in resources
                else httpx.Response(404)
            )
        if request.method == "DELETE":
            deleted.append(path)
            resources.pop(path, None)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected Kubernetes method: {request.method}")

    service_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"event_id": event["event_id"], "status": "succeeded"}
        )
    )
    reconciler = _reconciler(service_transport, httpx.MockTransport(kubernetes))

    assert reconciler.process_workload(event) == "succeeded"
    assert deleted == [service_url, deployment_url]
    assert not resources


def test_workload_delete_refuses_unowned_secondary_resources():
    event = _workload_event(operation="delete")
    resource = _owned_workload()
    resource["metadata"]["labels"].pop("codefactory.io/managed-inference-workload")
    acknowledgments = []

    def kubernetes(request):
        return httpx.Response(200, json=resource)

    def service(request):
        acknowledgments.append(request.read())
        return httpx.Response(
            200, json={"event_id": event["event_id"], "status": "retrying"}
        )

    result = _reconciler(
        httpx.MockTransport(service), httpx.MockTransport(kubernetes)
    ).process_workload(event)

    assert result == "retrying"
    assert b'"outcome":"failed"' in acknowledgments[0]


def test_workload_ensure_adopts_only_known_default_resources(tmp_path):
    event = _workload_event(instance_id="gpu")
    templates = tmp_path / "templates"
    _write_templates(templates)
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "llama-cpp-server",
            "labels": {"app.kubernetes.io/part-of": "inference-serving"},
            "generation": 1,
        },
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {
                    "labels": {"app.kubernetes.io/part-of": "inference-serving"}
                },
                "spec": {
                    "containers": [
                        {
                            "name": "llama-server",
                            "args": ["--model", "/models/approved.gguf"],
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    service = {
        "metadata": {
            "name": "llama-cpp-server",
            "labels": {"app.kubernetes.io/part-of": "inference-serving"},
        }
    }
    resources = {
        "/apis/apps/v1/namespaces/inference/deployments/llama-cpp-server": deployment,
        "/api/v1/namespaces/inference/services/llama-cpp-server": service,
    }
    patches = []

    def kubernetes(request):
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json=resources[path])
        if request.method == "PATCH":
            patch = json.loads(request.read())
            patches.append((path, patch))
            current = resources[path]
            if path.endswith("/deployments/llama-cpp-server"):
                current["metadata"].setdefault("labels", {}).update(
                    patch["metadata"]["labels"]
                )
                current["spec"]["template"]["metadata"].setdefault("labels", {}).update(
                    patch["spec"]["template"]["metadata"]["labels"]
                )
                current["metadata"]["generation"] = 2
                current["status"]["observedGeneration"] = 2
            else:
                current["metadata"].setdefault("labels", {}).update(
                    patch["metadata"]["labels"]
                )
            return httpx.Response(200, json=current)
        raise AssertionError(f"unexpected Kubernetes method: {request.method}")

    service_transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"event_id": event["event_id"], "status": "succeeded"}
        )
    )
    reconciler = _reconciler(
        service_transport,
        httpx.MockTransport(kubernetes),
        template_directory=templates,
    )

    assert reconciler.process_workload(event) == "succeeded"
    assert len(patches) == 2
    deployment_patch = next(
        patch
        for path, patch in patches
        if path.endswith("/deployments/llama-cpp-server")
    )
    assert deployment_patch["spec"]["template"]["spec"]["containers"][0]["args"] == [
        "--model",
        "/models/approved.gguf",
        "--ctx-size",
        "8192",
        "--threads",
        "3",
    ]
    assert all(
        patch["metadata"]["labels"]["codefactory.io/managed-inference-workload"]
        == "true"
        for _, patch in patches
    )
