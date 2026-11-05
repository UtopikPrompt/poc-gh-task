import json

import httpx
import pytest
from langgraph_service.providers.local_model import (
    CPU_LLAMA_CPP_URL,
    DEFAULT_LLAMA_CPP_URL,
    LocalLlamaCppProposalProvider,
    READINESS_TIMEOUT_SECONDS,
    SSE_PING_INTERVAL_SECONDS,
)


def _envelope(content, **overrides):
    return {
        "content": content,
        "stop": True,
        "truncated": False,
        "tokens_predicted": 12,
        **overrides,
    }


def _stream_response(*events):
    body = "".join(
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events
    )
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": "text/event-stream"},
    )


def test_provider_posts_deterministic_schema_request_to_internal_completion():
    requests = []

    def handle(request):
        requests.append(request)
        return _stream_response(
            {
                "content": '{"paths":',
                "tokens": [1],
                "stop": False,
                "tokens_predicted": 1,
            },
            {
                "content": '["src/task.py"]}',
                "tokens": [2],
                "stop": False,
                "tokens_predicted": 2,
            },
            {
                "content": "",
                "tokens": [],
                "stop": True,
                "truncated": False,
                "tokens_predicted": 2,
            },
        )

    provider = LocalLlamaCppProposalProvider(transport=httpx.MockTransport(handle))
    schema = {"type": "object", "required": ["paths"]}
    try:
        result = provider.complete(
            prompt="bounded prompt",
            response_schema=schema,
            max_tokens=1024,
        )
    finally:
        provider.close()

    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == f"{DEFAULT_LLAMA_CPP_URL}/completion"
    assert request.headers.get("Authorization") is None
    assert body == {
        "prompt": "bounded prompt",
        "temperature": 0,
        "top_k": 1,
        "top_p": 1,
        "n_predict": 1024,
        "stream": True,
        "cache_prompt": False,
        "json_schema": schema,
        "sse_ping_interval": SSE_PING_INTERVAL_SECONDS,
    }
    assert result == {"paths": ["src/task.py"]}


def test_provider_selects_curated_cpu_profile_endpoint():
    requests = []

    def handle(request):
        requests.append(request)
        return _stream_response(_envelope('{"ok":true}', tokens_predicted=1))

    provider = LocalLlamaCppProposalProvider(transport=httpx.MockTransport(handle))
    try:
        assert provider.complete(
            prompt="prompt",
            response_schema={"type": "object"},
            max_tokens=20,
            model_profile="qwen2_5_0_5b_cpu",
        ) == {"ok": True}
    finally:
        provider.close()

    assert str(requests[0].url) == f"{CPU_LLAMA_CPP_URL}/completion"


@pytest.mark.parametrize(
    ("envelope", "message"),
    [
        (_envelope("```json\n{}\n```"), "strict JSON"),
        (_envelope('{"paths":["src/a.py"],"paths":["src/b.py"]}'), "duplicate key"),
        (_envelope('{"ok":true}', stop=False), "truncated"),
        (_envelope('{"ok":true}', truncated=True), "truncated"),
        (_envelope('{"ok":true}', tokens_predicted=1024), "token limit"),
        ({"content": "{}", "stop": True, "tokens_predicted": 1}, "truncated"),
    ],
)
def test_provider_fails_closed_on_invalid_or_incomplete_responses(envelope, message):
    event = {"content": envelope.get("content", ""), "tokens": []}
    for field in ("stop", "truncated", "tokens_predicted"):
        if field in envelope:
            event[field] = envelope[field]
    provider = LocalLlamaCppProposalProvider(
        transport=httpx.MockTransport(lambda _: _stream_response(event))
    )
    try:
        with pytest.raises(ValueError, match=message):
            provider.complete(
                prompt="prompt",
                response_schema={"type": "object"},
                max_tokens=1024,
            )
    finally:
        provider.close()


def test_provider_rejects_non_object_content_external_urls_and_oversized_response():
    provider = LocalLlamaCppProposalProvider(
        transport=httpx.MockTransport(
            lambda _: _stream_response(
                {
                    "content": "[]",
                    "tokens": [],
                    "stop": True,
                    "truncated": False,
                    "tokens_predicted": 1,
                }
            )
        )
    )
    try:
        with pytest.raises(ValueError, match="JSON object"):
            provider.complete(
                prompt="prompt",
                response_schema={"type": "object"},
                max_tokens=1024,
            )
    finally:
        provider.close()

    with pytest.raises(ValueError, match="private or cluster-local"):
        LocalLlamaCppProposalProvider(base_url="https://inference.example.com")

    oversized = LocalLlamaCppProposalProvider(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b"data: " + b"x" * (512 * 1024 + 1) + b"\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    try:
        with pytest.raises(ValueError, match="response exceeds"):
            oversized.complete(
                prompt="prompt",
                response_schema={"type": "object"},
                max_tokens=1024,
            )
    finally:
        oversized.close()


@pytest.mark.parametrize(("status_code", "ready"), [(200, True), (503, False)])
def test_provider_readiness_probe_is_bounded_and_reports_health(status_code, ready):
    requests = []
    provider = LocalLlamaCppProposalProvider(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(status_code)
        )
    )
    try:
        assert provider.is_ready() is ready
    finally:
        provider.close()

    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/health"
    assert all(
        timeout <= READINESS_TIMEOUT_SECONDS
        for timeout in request.extensions["timeout"].values()
    )


def test_provider_readiness_reports_transport_outage():
    def fail(request):
        raise httpx.ConnectError("unavailable", request=request)

    provider = LocalLlamaCppProposalProvider(transport=httpx.MockTransport(fail))
    try:
        assert provider.is_ready() is False
    finally:
        provider.close()
