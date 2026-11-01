"""Bounded local llama.cpp completion provider for structured workflow output."""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

DEFAULT_LLAMA_CPP_URL = "http://llama-cpp-server.inference.svc.cluster.local:8080"
READINESS_TIMEOUT_SECONDS = 2.0
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
        if not 1 <= timeout_seconds <= 90:
            raise ValueError("local model timeout must be between 1 and 90 seconds")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds, connect=min(5, timeout_seconds)),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            trust_env=False,
        )

    @classmethod
    def from_environment(cls) -> LocalLlamaCppProposalProvider:
        return cls(base_url=os.environ.get("LLAMA_CPP_URL", DEFAULT_LLAMA_CPP_URL))

    def complete(
        self,
        *,
        prompt: str,
        response_schema: Mapping[str, object],
        max_tokens: int,
    ) -> dict[str, object]:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("model prompt must be non-empty text")
        if len(prompt.encode("utf-8")) > 96 * 1024:
            raise ValueError("model prompt exceeds the byte limit")
        if not 1 <= max_tokens <= MAX_COMPLETION_TOKENS:
            raise ValueError("model completion token limit is invalid")
        request_payload = {
            "prompt": prompt,
            "temperature": 0,
            "top_k": 1,
            "top_p": 1,
            "n_predict": max_tokens,
            "stream": False,
            "cache_prompt": False,
            "json_schema": dict(response_schema),
        }
        with self._client.stream(
            "POST", "/completion", json=request_payload
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > MAX_COMPLETION_RESPONSE_BYTES:
                    raise ValueError("local model response exceeds the byte limit")
                body.extend(chunk)
        try:
            result = _strict_json_loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("local model returned invalid response JSON") from error
        if not isinstance(result, Mapping):
            raise ValueError("local model returned an invalid completion envelope")
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
        content = result.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError("local model returned empty completion content")
        try:
            output = _strict_json_loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("local model content is not strict JSON") from error
        if not isinstance(output, dict):
            raise ValueError("local model content must be a JSON object")
        return output

    def is_ready(self) -> bool:
        try:
            with self._client.stream(
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
        self._client.close()


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
    if host in {"localhost"} or host.endswith((".svc", ".svc.cluster.local", ".local")):
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError(
            "local model URL host must be private or cluster-local"
        ) from error
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError("local model URL host must be private or cluster-local")
