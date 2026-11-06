"""Prometheus-backed, explicitly unavailable/stale inference metrics."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Callable, Literal, Sequence

import httpx

from langgraph_service.inference_config import InferenceInstance

MetricStatus = Literal["available", "stale", "unavailable"]


def _metric(value: float | None, status: MetricStatus) -> dict[str, object]:
    return {"value": value, "status": status}


class PrometheusInferenceMetrics:
    def __init__(
        self,
        prometheus_url: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        stale_after_seconds: int = 120,
        timeout_seconds: float = 5,
    ):
        self._url = prometheus_url.rstrip("/") if prometheus_url else ""
        self._transport = transport
        self._clock = clock
        self._stale_after = stale_after_seconds
        self._timeout = timeout_seconds

    def get_metrics(
        self, instance_ids: Sequence[InferenceInstance]
    ) -> dict[str, object]:
        instances = []
        for instance_id in instance_ids:
            pod = "llama-cpp-server-.+"
            selector = (
                f'namespace="inference",inference_instance="{instance_id}",'
                f'pod=~"{pod}",container="llama-server"'
            )
            samples = {
                "cpu_utilization_percent": self._query(
                    "100 * sum(rate(container_cpu_usage_seconds_total{"
                    + selector
                    + "}[5m])) / sum(kube_pod_container_resource_limits{"
                    + selector
                    + ',resource="cpu",unit="core"})'
                ),
                "ram_used_bytes": self._query(
                    "sum(container_memory_working_set_bytes{" + selector + "})"
                ),
            }
            if instance_id.startswith("gpu"):
                gpu_selector = (
                    f'namespace="inference",inference_instance="{instance_id}",'
                    f'pod=~"{pod}"'
                )
                samples.update(
                    {
                        "gpu_utilization_percent": self._query(
                            "avg(DCGM_FI_DEV_GPU_UTIL{" + gpu_selector + "})"
                        ),
                        "vram_used_bytes": self._query(
                            "sum(DCGM_FI_DEV_FB_USED{" + gpu_selector + "}) * 1048576"
                        ),
                        "vram_total_bytes": self._query(
                            "sum(DCGM_FI_DEV_FB_USED{"
                            + gpu_selector
                            + "} + DCGM_FI_DEV_FB_FREE{"
                            + gpu_selector
                            + "}) * 1048576"
                        ),
                    }
                )
            else:
                samples.update(
                    {
                        "gpu_utilization_percent": (None, None),
                        "vram_used_bytes": (None, None),
                        "vram_total_bytes": (None, None),
                    }
                )
            now = self._clock()
            values: dict[str, dict[str, object]] = {}
            timestamps = []
            for name, (value, timestamp) in samples.items():
                if timestamp is None or value is None:
                    values[name] = _metric(None, "unavailable")
                elif timestamp > now + 5 or not math.isfinite(value):
                    values[name] = _metric(None, "unavailable")
                else:
                    timestamps.append(timestamp)
                    status: MetricStatus = (
                        "stale" if now - timestamp > self._stale_after else "available"
                    )
                    values[name] = _metric(value, status)
            sampled_at = (
                datetime.fromtimestamp(max(timestamps), timezone.utc).isoformat()
                if timestamps
                else None
            )
            instances.append(
                {"instance_id": instance_id, **values, "sampled_at": sampled_at}
            )
        return {"instances": instances}

    def _query(self, expression: str) -> tuple[float | None, float | None]:
        if not self._url:
            return None, None
        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = client.get(
                    f"{self._url}/api/v1/query", params={"query": expression}
                )
            response.raise_for_status()
            payload = response.json()
            results = payload["data"]["result"]
            if payload.get("status") != "success" or not results:
                return None, None
            timestamp, raw_value = results[0]["value"]
            value = float(raw_value)
            sample_time = float(timestamp)
            if not (value >= 0 and sample_time > 0):
                return None, None
            return value, sample_time
        except Exception:
            return None, None
