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
