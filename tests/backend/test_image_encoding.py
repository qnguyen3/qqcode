from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

from vibe.core.config import ModelCapabilities, ModelConfig, ProviderConfig
from vibe.core.images import (
    MAX_IMAGE_BYTES,
    ImageError,
    describe_image,
    load_image_part,
    media_type_for,
    read_image_bytes,
    resolve_messages,
)
from vibe.core.llm.backend.adapters.anthropic_messages import AnthropicAdapter
from vibe.core.llm.backend.adapters.openai_chat import OpenAIAdapter
from vibe.core.llm.backend.generic import GenericBackend
from vibe.core.types import ImagePart, LLMMessage, Role, message_for_storage

BASE_URL = "https://api.test-provider.example"

CHAT_RESPONSE = {
    "id": "chatcmpl_test",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "model_name",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def write_png(path: Path, *, size: int = 0) -> Path:
    path.write_bytes(PNG_BYTES + b"\x00" * size)
    return path


def make_provider(api_style: str) -> ProviderConfig:
    return ProviderConfig(
        name="provider_name",
        api_base="https://api.test-provider.example/v1",
        api_key_env_var="API_KEY",
        api_style=api_style,
    )


def make_model(*, vision: bool) -> ModelConfig:
    return ModelConfig(
        name="model_name",
        provider="provider_name",
        alias="model_alias",
        capabilities=ModelCapabilities(vision=vision),
    )


def adapter_request(adapter, model: ModelConfig, messages: list[LLMMessage]) -> dict:
    request = adapter.prepare_request(
        model_name=model.name,
        messages=messages,
        temperature=0.2,
        tools=None,
        max_tokens=None,
        tool_choice=None,
        enable_streaming=False,
        provider=make_provider("openai"),
        api_key="test-key",
    )
    return json.loads(request.body)


def test_media_type_for_supported_extensions(tmp_path: Path) -> None:
    assert media_type_for(Path("photo.PNG")) == "image/png"
    assert media_type_for(Path("photo.jpeg")) == "image/jpeg"
    assert media_type_for(Path("notes.txt")) is None


def test_describe_image_returns_a_reference(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")

    image = describe_image(path)

    assert image.media_type == "image/png"
    assert image.source_path == str(path)
    assert image.size_bytes == len(PNG_BYTES)
    assert image.data == ""


def test_describe_image_rejects_unsupported_files(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(ImageError):
        describe_image(path)


def test_read_image_bytes_enforces_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without Pillow an oversized image cannot be resized, so reading must fail.
    monkeypatch.setattr("vibe.core.images._pillow_image_module", lambda: None)
    path = write_png(tmp_path / "huge.png", size=MAX_IMAGE_BYTES + 1)

    with pytest.raises(ImageError, match="Pillow"):
        read_image_bytes(path)


def test_load_image_part_reads_base64(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    ref = describe_image(path)

    loaded = load_image_part(ref)

    assert loaded.is_loaded
    assert base64.b64decode(loaded.data) == PNG_BYTES
    assert loaded.data_uri().startswith("data:image/png;base64,")


def test_load_image_part_missing_file_raises(tmp_path: Path) -> None:
    ref = ImagePart(media_type="image/png", source_path=str(tmp_path / "gone.png"))

    with pytest.raises(ImageError, match="Could not read image"):
        load_image_part(ref)


def test_resolve_messages_downgrades_without_vision(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(role=Role.user, content="look", images=[describe_image(path)])

    resolved = resolve_messages([message], vision=False)

    assert resolved[0].images == []
    assert "look" in (resolved[0].content or "")
    assert "[image image/png" in (resolved[0].content or "")


def test_resolve_messages_loads_with_vision(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(role=Role.user, content="look", images=[describe_image(path)])

    resolved = resolve_messages([message], vision=True)

    assert resolved[0].images[0].is_loaded
    assert base64.b64decode(resolved[0].images[0].data) == PNG_BYTES


def test_resolve_messages_missing_file_becomes_placeholder(tmp_path: Path) -> None:
    message = LLMMessage(
        role=Role.user,
        content="look",
        images=[
            ImagePart(media_type="image/png", source_path=str(tmp_path / "gone.png"))
        ],
    )

    resolved = resolve_messages([message], vision=True)

    assert resolved[0].images == []
    assert "[image unavailable" in (resolved[0].content or "")


def test_message_for_storage_drops_base64(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(role=Role.user, images=[load_image_part(describe_image(path))])

    stored = message_for_storage(message)

    assert stored.images[0].data == ""
    assert stored.images[0].source_path == str(path)


def test_openai_adapter_encodes_images(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(
        role=Role.user, content="look", images=[load_image_part(describe_image(path))]
    )

    payload = adapter_request(OpenAIAdapter(), make_model(vision=True), [message])
    content = payload["messages"][0]["content"]

    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_anthropic_adapter_encodes_images(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(
        role=Role.user, content="look", images=[load_image_part(describe_image(path))]
    )

    payload = adapter_request(AnthropicAdapter(), make_model(vision=True), [message])
    content = payload["messages"][0]["content"]

    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == PNG_BYTES


async def run_backend_request(*, vision: bool, message: LLMMessage) -> dict:
    with respx.mock(base_url=BASE_URL) as mock_api:
        route = mock_api.post("/v1/chat/completions").mock(
            return_value=httpx.Response(status_code=200, json=CHAT_RESPONSE)
        )
        backend = GenericBackend(provider=make_provider("openai"))
        await backend.complete(
            model=make_model(vision=vision),
            messages=[message],
            temperature=0.2,
            tools=None,
            max_tokens=None,
            tool_choice=None,
            extra_headers=None,
        )
        return json.loads(route.calls[0].request.content)


@pytest.mark.asyncio
async def test_backend_sends_image_parts_for_vision_models(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(role=Role.user, content="look", images=[describe_image(path)])

    payload = await run_backend_request(vision=True, message=message)

    content = payload["messages"][0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_backend_downgrades_images_for_text_models(tmp_path: Path) -> None:
    path = write_png(tmp_path / "pixel.png")
    message = LLMMessage(role=Role.user, content="look", images=[describe_image(path)])

    payload = await run_backend_request(vision=False, message=message)

    content = payload["messages"][0]["content"]
    assert isinstance(content, str)
    assert "[image image/png" in content
    assert "image_url" not in json.dumps(payload)
