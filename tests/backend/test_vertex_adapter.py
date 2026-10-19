from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from vibe.core import ambient
from vibe.core.config import ModelConfig, ProviderConfig
from vibe.core.llm.backend.adapters import get_adapter
from vibe.core.llm.backend.adapters.google_vertex import GoogleVertexAdapter
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import LLMMessage, Role

VERTEX_BASE = "https://us-central1-aiplatform.googleapis.com"
VERTEX_API_BASE = f"{VERTEX_BASE}/v1/projects/demo-project/locations/us-central1"

GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "Hi"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
}


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="google-vertex",
        api_base="https://aiplatform.googleapis.com/v1",
        api_style="google-vertex",
        ambient="google-vertex",
    )


def _model() -> ModelConfig:
    return ModelConfig(
        name="gemini-2.5-flash", provider="google-vertex", alias="vertex/gemini"
    )


def test_vertex_adapter_is_registered() -> None:
    assert isinstance(get_adapter("google-vertex"), GoogleVertexAdapter)


def test_vertex_adapter_uses_the_publisher_endpoint() -> None:
    adapter = get_adapter("google-vertex")

    request = adapter.prepare_request(
        model_name="gemini-2.5-flash",
        messages=[LLMMessage(role=Role.user, content="hi")],
        temperature=0.2,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=ProviderConfig(
            name="google-vertex",
            api_base=VERTEX_API_BASE,
            api_style="google-vertex",
            ambient="google-vertex",
        ),
    )

    assert request.endpoint == (
        "/publishers/google/models/gemini-2.5-flash:generateContent"
    )


@pytest.mark.asyncio
async def test_generic_backend_vertex_uses_adc_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "adc.json"
    credentials.write_text(json.dumps({"type": "authorized_user"}), encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)

    async def fake_token(path: Path | None = None) -> str:
        return "fake-adc-token"

    monkeypatch.setattr(ambient, "vertex_access_token", fake_token)

    with respx.mock(base_url=VERTEX_BASE) as mock_api:
        route = mock_api.post(
            "/v1/projects/demo-project/locations/us-central1"
            "/publishers/google/models/gemini-2.5-flash:generateContent"
        ).mock(return_value=httpx.Response(status_code=200, json=GEMINI_RESPONSE))

        backend = GenericBackend(provider=_provider())
        result = await backend.complete(
            model=_model(),
            messages=[LLMMessage(role=Role.user, content="hi")],
            temperature=0.2,
            tools=None,
            max_tokens=None,
            tool_choice=None,
            extra_headers=None,
        )

    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer fake-adc-token"
    payload = json.loads(request.content)
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert result.message.content == "Hi"
    assert result.finish_reason == "stop"
