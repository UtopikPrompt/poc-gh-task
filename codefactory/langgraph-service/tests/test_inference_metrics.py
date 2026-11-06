import time

import httpx

from langgraph_service.inference_metrics import PrometheusInferenceMetrics


def test_missing_prometheus_configuration_returns_unavailable_without_fetching():
    metrics = PrometheusInferenceMetrics(None)
    result = metrics.get_metrics(("gpu", "cpu", "gpu-secondary", "cpu-secondary"))

    assert [item["instance_id"] for item in result["instances"]] == [
        "gpu",
        "cpu",
        "gpu-secondary",
        "cpu-secondary",
    ]
    assert all(
        metric["status"] == "unavailable" and metric["value"] is None
        for instance in result["instances"]
        for name, metric in instance.items()
        if name.endswith(("percent", "bytes"))
    )
    assert all(instance["sampled_at"] is None for instance in result["instances"])


def test_prometheus_values_are_reported_with_per_sample_freshness():
    now = time.time()
    queries = []

    def respond(request):
        query = request.url.params["query"]
        queries.append(query)
        if "container_cpu_usage_seconds_total" in query:
            value = "35"
        elif "container_memory_working_set_bytes" in query:
            value = "1073741824"
        elif "DCGM_FI_DEV_GPU_UTIL" in query:
            value = "73"
        elif "DCGM_FI_DEV_FB_USED" in query and "FB_FREE" in query:
            value = "8589934592"
        else:
            value = "4294967296"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"value": [now, value]}]},
            },
        )

    metrics = PrometheusInferenceMetrics(
        "http://prometheus:9090",
        transport=httpx.MockTransport(respond),
        clock=lambda: now,
    )
    result = metrics.get_metrics(("gpu", "cpu", "gpu-secondary", "cpu-secondary"))
    gpu, cpu, secondary_gpu, secondary_cpu = result["instances"]

    assert gpu["cpu_utilization_percent"] == {"value": 35.0, "status": "available"}
    assert gpu["ram_used_bytes"] == {"value": 1073741824.0, "status": "available"}
    assert gpu["gpu_utilization_percent"] == {"value": 73.0, "status": "available"}
    assert gpu["vram_used_bytes"]["value"] == 4294967296.0
    assert gpu["vram_total_bytes"]["value"] == 8589934592.0
    assert cpu["gpu_utilization_percent"] == {"value": None, "status": "unavailable"}
    assert cpu["vram_total_bytes"] == {"value": None, "status": "unavailable"}
    assert secondary_gpu["gpu_utilization_percent"]["value"] == 73.0
    assert secondary_cpu["gpu_utilization_percent"]["status"] == "unavailable"
    assert gpu["sampled_at"] is not None
    assert len(queries) == 14
    assert all('pod=~"llama-cpp-server-.+"' in query for query in queries)
    assert all('inference_instance="gpu"' in query for query in queries[:5])
    assert all('inference_instance="cpu"' in query for query in queries[5:7])
    assert all('inference_instance="gpu-secondary"' in query for query in queries[7:12])
    assert all('inference_instance="cpu-secondary"' in query for query in queries[12:])
    assert any("container_cpu_usage_seconds_total" in query for query in queries)
    assert any("container_memory_working_set_bytes" in query for query in queries)
    assert any("DCGM_FI_DEV_GPU_UTIL" in query for query in queries)
    assert any("DCGM_FI_DEV_FB_USED" in query for query in queries)
    assert any("DCGM_FI_DEV_FB_FREE" in query for query in queries)


def test_old_prometheus_samples_are_labeled_stale():
    old = time.time() - 600
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"value": [old, "10"]}]},
            },
        )
    )
    metrics = PrometheusInferenceMetrics(
        "http://prometheus:9090", transport=transport, clock=lambda: old + 600
    )

    result = metrics.get_metrics(("gpu",))

    assert result["instances"][0]["cpu_utilization_percent"] == {
        "value": 10.0,
        "status": "stale",
    }
