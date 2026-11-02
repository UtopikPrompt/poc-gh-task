import json

import httpx
import pytest
from langgraph_service.local_model import (
    DEFAULT_LLAMA_CPP_URL,
    LocalLlamaCppProposalProvider,
    READINESS_TIMEOUT_SECONDS,
)


def _envelope(content, **overrides):
    return {
        "content": content,
        "stop": True,
        "truncated": False,
        "tokens_predicted": 12,
        **overrides,
    }


def test_provider_posts_deterministic_schema_request_to_internal_completion():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=_envelope('{"paths":["src/task.py"]}'))

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
        "stream": False,
        "cache_prompt": False,
        "json_schema": schema,
    }
    assert result == {"paths": ["src/task.py"]}


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
    provider = LocalLlamaCppProposalProvider(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope))
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
            lambda _: httpx.Response(200, json=_envelope("[]"))
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
            lambda _: httpx.Response(200, content=b"x" * (512 * 1024 + 1))
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
