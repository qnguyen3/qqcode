from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
import logging
import os
import types
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx

from vibe.core.config import (
    ThinkingLevel,
    resolve_thinking_level,
    save_oauth_token,
    thinking_effort,
)
from vibe.core.images import resolve_messages
from vibe.core.llm.backend.adapters import (
    ADAPTER_REGISTRY,
    BACKEND_ADAPTERS,
    AnthropicAdapter,
    APIAdapter,
    BedrockConverseAdapter,
    GoogleGenerativeAIAdapter,
    GoogleVertexAdapter,
    OpenAIAdapter,
    OpenAICodexResponsesAdapter,
    OpenAIResponsesAdapter,
    PiMessagesAdapter,
    PreparedRequest,
    UnknownAPIStyleError,
    get_adapter,
    register_adapter,
)
from vibe.core.llm.cache import CachePlan
from vibe.core.llm.capabilities import clamp_max_tokens
from vibe.core.llm.exceptions import BackendErrorBuilder
from vibe.core.oauth.registry import get_flow
from vibe.core.types import (
    AvailableTool,
    LLMChunk,
    LLMMessage,
    Role,
    StrToolChoice,
    ToolCall,
)
from vibe.core.utils import _retry_after_seconds, async_generator_retry, async_retry

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig
    from vibe.core.oauth.token import OAuthToken

logger = logging.getLogger(__name__)


def drop_temperature(body: bytes) -> bytes:
    """Remove temperature from a JSON request body for a model that rejects it."""
    payload = json.loads(body)
    payload.pop("temperature", None)
    return json.dumps(payload).encode("utf-8")


def _normalize_qwen_resource_url(resource_url: str | None) -> str | None:
    """Ensure a Qwen resource URL carries the https:// prefix and /v1 suffix."""
    if not resource_url:
        return None
    url = resource_url if resource_url.startswith("http") else f"https://{resource_url}"
    return url if url.endswith("/v1") else f"{url}/v1"


__all__ = [
    "ADAPTER_REGISTRY",
    "BACKEND_ADAPTERS",
    "APIAdapter",
    "AnthropicAdapter",
    "BedrockConverseAdapter",
    "GenericBackend",
    "GoogleGenerativeAIAdapter",
    "GoogleVertexAdapter",
    "OpenAIAdapter",
    "OpenAICodexResponsesAdapter",
    "OpenAIResponsesAdapter",
    "PiMessagesAdapter",
    "PreparedRequest",
    "UnknownAPIStyleError",
    "get_adapter",
    "register_adapter",
]


class GenericBackend:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        provider: ProviderConfig,
        timeout: float = 720.0,
        honor_retry_after: bool = True,
    ) -> None:
        """Initialize the backend.

        Args:
            client: Optional httpx client to use. If not provided, one will be created.
            honor_retry_after: Whether server Retry-After delays are honored.
        """
        self._client = client
        self._owns_client = client is None
        self._provider = provider
        self._timeout = timeout
        self._refresh_locks: dict[str, asyncio.Lock] = {}

        retry_after_parser = _retry_after_seconds if honor_retry_after else None
        self._make_request = async_retry(tries=3, get_retry_after=retry_after_parser)(
            self._request_once
        )
        self._make_streaming_request = async_generator_retry(
            tries=3, get_retry_after=retry_after_parser
        )(self._stream_once)

    async def __aenter__(self) -> GenericBackend:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
            self._owns_client = True
        return self._client

    async def _ensure_fresh_token(self) -> None:
        """Refresh the provider token once, even under concurrent requests."""
        token = self._provider.oauth_token
        if token is None or not token.is_expired(skew=60):
            return

        lock = self._refresh_locks.setdefault(self._provider.name, asyncio.Lock())
        async with lock:
            token = self._provider.oauth_token
            if token is None or not token.is_expired(skew=60):
                return
            await self._refresh_oauth_token(token)

    async def _refresh_oauth_token(self, token: OAuthToken) -> None:
        """Refresh the expired OAuth token through its registered flow."""
        try:
            flow = get_flow(self._provider.oauth_flow or self._provider.name)
            new_token = await flow.refresh(token)
            # Update in-memory token
            self._provider.oauth_token = new_token
            # Persist to config
            save_oauth_token(self._provider.name, new_token)
        except Exception:
            # If refresh fails, keep the token so we fall back to the API key
            logger.debug(
                "OAuth refresh failed for provider %s",
                self._provider.name,
                exc_info=True,
            )

    async def _get_auth_info(self) -> tuple[str | None, dict[str, str], str | None]:
        """Get API key, extra headers, and optional API base override.

        Returns:
            Tuple of (api_key, extra_headers, api_base_override) where:
            - api_key may be a Bearer token for OAuth authentication
            - api_base_override is the URL to use instead of provider.api_base (for Qwen OAuth)
        """
        if self._provider.oauth_token:
            # Refresh token if expired (serialized per provider)
            await self._ensure_fresh_token()
            oauth = self._provider.oauth_token
            if oauth and not oauth.is_expired(skew=60):
                return self._oauth_auth_info(oauth.access_token)

        if self._provider.ambient:
            from vibe.core.ambient import resolve_ambient_auth

            credential = await resolve_ambient_auth(self._provider.ambient)
            if credential is not None:
                return (
                    credential.api_key,
                    dict(credential.headers),
                    credential.api_base,
                )

        # Fall back to API key from env var
        api_key = (
            os.getenv(self._provider.api_key_env_var)
            if self._provider.api_key_env_var
            else None
        )
        return api_key, {}, None

    def _oauth_auth_info(
        self, access_token: str
    ) -> tuple[str, dict[str, str], str | None]:
        """Build the auth tuple for a valid OAuth token."""
        match self._provider.name:
            case "anthropic":
                return (
                    access_token,
                    {
                        "anthropic-version": "2023-06-01",
                        "anthropic-beta": "oauth-2025-04-20",
                    },
                    None,
                )
            case "qwen":
                resource_url = getattr(self._provider.oauth_token, "resource_url", None)
                return access_token, {}, _normalize_qwen_resource_url(resource_url)
            case _:
                return access_token, {}, None

    def _resolve_thinking_level(
        self, model: ModelConfig, thinking_level: ThinkingLevel | None
    ) -> ThinkingLevel | None:
        return resolve_thinking_level(model, thinking_level)

    def _thinking_request(
        self, model: ModelConfig, thinking_level: ThinkingLevel | None
    ) -> dict[str, Any]:
        """Return the thinking arguments for an adapter's ``prepare_request``."""
        level = self._resolve_thinking_level(model, thinking_level)
        return {
            "thinking_level": level,
            "reasoning_effort": thinking_effort(model, level),
        }

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        tools: list[AvailableTool] | None = None,
        max_tokens: int | None = None,
        tool_choice: StrToolChoice | AvailableTool | None = None,
        extra_headers: dict[str, str] | None = None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> LLMChunk:
        api_key, oauth_headers, api_base_override = await self._get_auth_info()

        api_style = model.api_style or self._provider.api_style
        adapter = get_adapter(api_style)

        if getattr(adapter, "streaming_only", False):
            return await self._aggregate_stream(
                model=model,
                messages=messages,
                temperature=temperature,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                extra_headers=extra_headers,
                cache_plan=cache_plan,
                thinking_level=thinking_level,
            )

        extra_body = getattr(model, "extra_body", None) or None
        if not model.capabilities.tools:
            tools = None
            tool_choice = None
        elif not model.capabilities.tool_choice:
            tool_choice = None

        endpoint, headers, body = adapter.prepare_request(
            model_name=model.name,
            messages=resolve_messages(messages, vision=model.capabilities.vision),
            temperature=temperature,
            tools=tools,
            max_tokens=clamp_max_tokens(model, max_tokens),
            tool_choice=tool_choice,
            enable_streaming=False,
            provider=self._provider,
            api_key=api_key,
            extra_body=extra_body,
            cache_plan=cache_plan,
            **self._thinking_request(model, thinking_level),
        )
        if not model.capabilities.accepts_temperature:
            body = drop_temperature(body)

        # Apply OAuth headers first, then extra_headers can override
        headers.update(oauth_headers)
        if extra_headers:
            headers.update(extra_headers)

        api_base = api_base_override or self._provider.api_base
        url = f"{api_base}{endpoint}"

        try:
            res_data, _ = await self._make_request(url, body, headers)
            return adapter.postprocess_response(adapter.parse_response(res_data))

        except httpx.HTTPStatusError as e:
            raise BackendErrorBuilder.build_http_error(
                provider=self._provider.name,
                endpoint=url,
                response=e.response,
                headers=dict(e.response.headers.items()),
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e
        except httpx.RequestError as e:
            raise BackendErrorBuilder.build_request_error(
                provider=self._provider.name,
                endpoint=url,
                error=e,
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e

    async def _aggregate_stream(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
        cache_plan: CachePlan | None,
        thinking_level: ThinkingLevel | None,
    ) -> LLMChunk:
        """Serve a non-streaming caller from an SSE-only adapter."""
        chunks = [
            chunk
            async for chunk in self.complete_streaming(
                model=model,
                messages=messages,
                temperature=temperature,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                extra_headers=extra_headers,
                cache_plan=cache_plan,
                thinking_level=thinking_level,
            )
        ]
        content = "".join(chunk.message.content or "" for chunk in chunks)
        reasoning = "".join(chunk.message.reasoning_content or "" for chunk in chunks)
        usage = next(
            (chunk.usage for chunk in reversed(chunks) if chunk.usage is not None), None
        )
        finish_reason = next(
            (
                chunk.finish_reason
                for chunk in chunks
                if chunk.finish_reason is not None
            ),
            None,
        )
        return LLMChunk(
            message=LLMMessage(
                role=Role.assistant,
                content=content or None,
                reasoning_content=reasoning or None,
                tool_calls=self._merge_tool_calls(chunks),
            ),
            usage=usage,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _merge_tool_calls(chunks: list[LLMChunk]) -> list[ToolCall] | None:
        merged: dict[int, ToolCall] = {}
        for chunk in chunks:
            for tool_call in chunk.message.tool_calls or []:
                index = tool_call.index
                if index is None:
                    merged[len(merged)] = tool_call
                    continue
                if index not in merged:
                    merged[index] = tool_call
                    continue
                merged[index].function.arguments = (
                    merged[index].function.arguments or ""
                ) + (tool_call.function.arguments or "")
        return list(merged.values()) or None

    async def complete_streaming(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        tools: list[AvailableTool] | None = None,
        max_tokens: int | None = None,
        tool_choice: StrToolChoice | AvailableTool | None = None,
        extra_headers: dict[str, str] | None = None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> AsyncGenerator[LLMChunk, None]:
        api_key, oauth_headers, api_base_override = await self._get_auth_info()

        api_style = model.api_style or self._provider.api_style
        adapter = get_adapter(api_style)

        if getattr(adapter, "non_streaming_only", False):
            yield await self.complete(
                model=model,
                messages=messages,
                temperature=temperature,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                extra_headers=extra_headers,
                cache_plan=cache_plan,
                thinking_level=thinking_level,
            )
            return

        extra_body = getattr(model, "extra_body", None) or None
        if not model.capabilities.tools:
            tools = None
            tool_choice = None
        elif not model.capabilities.tool_choice:
            tool_choice = None

        endpoint, headers, body = adapter.prepare_request(
            model_name=model.name,
            messages=resolve_messages(messages, vision=model.capabilities.vision),
            temperature=temperature,
            tools=tools,
            max_tokens=clamp_max_tokens(model, max_tokens),
            tool_choice=tool_choice,
            enable_streaming=True,
            provider=self._provider,
            api_key=api_key,
            extra_body=extra_body,
            cache_plan=cache_plan,
            **self._thinking_request(model, thinking_level),
        )
        if not model.capabilities.accepts_temperature:
            body = drop_temperature(body)

        # Apply OAuth headers first, then extra_headers can override
        headers.update(oauth_headers)
        if extra_headers:
            headers.update(extra_headers)

        api_base = api_base_override or self._provider.api_base
        url = f"{api_base}{endpoint}"

        try:
            async for res_data in self._make_streaming_request(url, body, headers):
                yield adapter.postprocess_response(adapter.parse_response(res_data))

        except httpx.HTTPStatusError as e:
            raise BackendErrorBuilder.build_http_error(
                provider=self._provider.name,
                endpoint=url,
                response=e.response,
                headers=dict(e.response.headers.items()),
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e
        except httpx.RequestError as e:
            raise BackendErrorBuilder.build_request_error(
                provider=self._provider.name,
                endpoint=url,
                error=e,
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e

    class HTTPResponse(NamedTuple):
        data: dict[str, Any]
        headers: dict[str, str]

    async def _request_once(
        self, url: str, data: bytes, headers: dict[str, str]
    ) -> HTTPResponse:
        client = self._get_client()
        response = await client.post(url, content=data, headers=headers)
        response.raise_for_status()

        response_headers = dict(response.headers.items())
        response_body = response.json()
        return self.HTTPResponse(response_body, response_headers)

    async def _stream_once(
        self, url: str, data: bytes, headers: dict[str, str]
    ) -> AsyncGenerator[dict[str, Any]]:
        client = self._get_client()
        async with client.stream(
            method="POST", url=url, content=data, headers=headers
        ) as response:
            if response.is_error:
                # Read the body so error reporting can include the provider
                # message; httpx forbids reading it after the stream closes.
                await response.aread()
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.strip() == "":
                    continue

                DELIM_CHAR = ":"
                assert f"{DELIM_CHAR} " in line, "line should look like `key: value`"
                delim_index = line.find(DELIM_CHAR)
                key = line[0:delim_index]
                value = line[delim_index + 2 :]

                if key != "data":
                    # This might be the case with openrouter, so we just ignore it
                    continue
                if value == "[DONE]":
                    return
                yield json.loads(value.strip())

    async def count_tokens(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        tools: list[AvailableTool] | None = None,
        tool_choice: StrToolChoice | AvailableTool | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> int:
        probe_messages = list(messages)
        if not probe_messages or probe_messages[-1].role != Role.user:
            probe_messages.append(LLMMessage(role=Role.user, content=""))

        result = await self.complete(
            model=model,
            messages=probe_messages,
            temperature=temperature,
            tools=tools,
            max_tokens=16,  # Minimal amount for openrouter with openai models
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        )
        assert result.usage is not None, (
            "Usage should be present in non-streaming completions"
        )

        return result.usage.prompt_tokens

    async def close(self) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None
