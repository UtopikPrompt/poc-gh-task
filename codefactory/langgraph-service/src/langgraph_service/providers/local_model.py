"""Bounded local llama.cpp completion provider for structured workflow output."""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlparse

import httpx

DEFAULT_LLAMA_CPP_URL = "http://llama-cpp-server.inference.svc.cluster.local:8080"
CPU_LLAMA_CPP_URL = "http://llama-cpp-server-cpu:8081"
DEFAULT_MODEL_PROFILE = "qwen2_5_32b_gpu"
ModelProfile = Literal["qwen2_5_32b_gpu", "qwen2_5_0_5b_cpu"]
READINESS_TIMEOUT_SECONDS = 2.0
SSE_PING_INTERVAL_SECONDS = 15
MAX_COMPLETION_RESPONSE_BYTES = 512 * 1024
MAX_COMPLETION_TOKENS = 4096


class LocalLlamaCppProposalProvider:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_LLAMA_CPP_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 45.0,
    ):
        _validate_local_url(base_url)
        _validate_local_url(CPU_LLAMA_CPP_URL)
        if not 1 <= timeout_seconds <= 90:
            raise ValueError("local model timeout must be between 1 and 90 seconds")
        self._clients = {
            "qwen2_5_32b_gpu": self._make_client(base_url, transport, timeout_seconds),
            "qwen2_5_0_5b_cpu": self._make_client(
                CPU_LLAMA_CPP_URL, transport, timeout_seconds
            ),
        }

    @staticmethod
    def _make_client(
        base_url: str,
        transport: httpx.BaseTransport | None,
        timeout_seconds: float,
    ) -> httpx.Client:
        return httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds)),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            trust_env=False,
        )

    def _client_for(self, model_profile: ModelProfile) -> httpx.Client:
        try:
            return self._clients[model_profile]
        except KeyError as error:
            raise ValueError("unsupported local model profile") from error

    @classmethod
    def from_environment(cls) -> LocalLlamaCppProposalProvider:
        return cls(base_url=os.environ.get("LLAMA_CPP_URL", DEFAULT_LLAMA_CPP_URL))

    def complete(
        self,
        *,
        prompt: str,
        response_schema: Mapping[str, object],
        max_tokens: int,
        model_profile: ModelProfile = DEFAULT_MODEL_PROFILE,
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("model prompt must be non-empty text")
        if len(prompt.encode("utf-8")) > 96 * 1024:
            raise ValueError("model prompt exceeds the byte limit")
        if not 1 <= max_tokens <= MAX_COMPLETION_TOKENS:
            raise ValueError("model completion token limit is invalid")
        if timeout_seconds is not None and not 1 <= timeout_seconds <= 90:
            raise ValueError("local model timeout must be between 1 and 90 seconds")
        request_payload = {
            "prompt": prompt,
            "temperature": 0,
            "top_k": 1,
            "top_p": 1,
            "n_predict": max_tokens,
            "stream": True,
            "cache_prompt": False,
            "json_schema": dict(response_schema),
            "sse_ping_interval": SSE_PING_INTERVAL_SECONDS,
        }
        request_options = (
            {"timeout": httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds))}
            if timeout_seconds is not None
            else {}
        )
        with self._client_for(model_profile).stream(
            "POST", "/completion", json=request_payload, **request_options
        ) as response:
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise ValueError("local model returned an invalid streaming response")
            content, result = _read_streaming_completion(response)
        if result.get("truncated") is not False or result.get("stop") is not True:
            raise ValueError("local model completion was truncated or did not stop")
        predicted = result.get("tokens_predicted")
        if (
            not isinstance(predicted, int)
            or isinstance(predicted, bool)
            or predicted < 0
            or predicted >= max_tokens
        ):
            raise ValueError("local model completion reached its token limit")
        if not content:
            raise ValueError("local model returned empty completion content")
        try:
            output = _strict_json_loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("local model content is not strict JSON") from error
        if not isinstance(output, dict):
            raise ValueError("local model content must be a JSON object")
        return output

    def is_ready(self, model_profile: ModelProfile = DEFAULT_MODEL_PROFILE) -> bool:
        try:
            with self._client_for(model_profile).stream(
                "GET",
                "/health",
                timeout=httpx.Timeout(
                    READINESS_TIMEOUT_SECONDS,
                    connect=min(1, READINESS_TIMEOUT_SECONDS),
                ),
            ) as response:
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        for client in self._clients.values():
            client.close()


def _read_streaming_completion(
    response: httpx.Response,
) -> tuple[str, Mapping[str, object]]:
    total_bytes = 0
    data_lines: list[str] = []
    content_parts: list[str] = []
    last_event: Mapping[str, object] | None = None

    def consume_event() -> None:
        nonlocal last_event
        if not data_lines:
            return
        data = "\n".join(data_lines)
        data_lines.clear()
        try:
            event = _strict_json_loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise ValueError(
                "local model returned an invalid streaming event"
            ) from error
        if not isinstance(event, Mapping):
            raise ValueError("local model returned an invalid streaming event")
        fragment = event.get("content")
        stopped = event.get("stop")
        if not isinstance(fragment, str) or not isinstance(stopped, bool):
            raise ValueError("local model returned an invalid streaming event")
        content_parts.append(fragment)
        last_event = event

    for line in response.iter_lines():
        total_bytes += len(line.encode("utf-8")) + 1
        if total_bytes > MAX_COMPLETION_RESPONSE_BYTES:
            raise ValueError("local model response exceeds the byte limit")
        if not line:
            consume_event()
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    consume_event()
    if last_event is None:
        raise ValueError("local model returned an incomplete streaming response")
    return "".join(content_parts), last_event


def _strict_json_loads(value: str | bytearray) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("local model JSON contains a duplicate key")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"local model JSON contains non-standard constant {value}")

    return json.loads(
        value,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def _validate_local_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("local model URL must be an internal service origin")
    if host in {"localhost", "llama-cpp-server-cpu"} or host.endswith(
        (".svc", ".svc.cluster.local", ".local")
    ):
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError(
            "local model URL host must be private or cluster-local"
        ) from error
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError("local model URL host must be private or cluster-local")
