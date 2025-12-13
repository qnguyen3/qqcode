from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
import json
import os
import types
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Protocol, TypeVar

import httpx

from vibe.core.llm.exceptions import BackendErrorBuilder
from vibe.core.types import (
    AvailableTool,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
)
from vibe.core.utils import async_generator_retry, async_retry

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig


class PreparedRequest(NamedTuple):
    endpoint: str
    headers: dict[str, str]
    body: bytes


class APIAdapter(Protocol):
    endpoint: ClassVar[str]

    def prepare_request(
        self,
        *,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        enable_streaming: bool,
        provider: ProviderConfig,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> PreparedRequest: ...

    def parse_response(self, data: dict[str, Any]) -> LLMChunk: ...


BACKEND_ADAPTERS: dict[str, APIAdapter] = {}

T = TypeVar("T", bound=APIAdapter)


def register_adapter(
    adapters: dict[str, APIAdapter], name: str
) -> Callable[[type[T]], type[T]]:

    def decorator(cls: type[T]) -> type[T]:
        adapters[name] = cls()
        return cls

    return decorator


@register_adapter(BACKEND_ADAPTERS, "openai")
class OpenAIAdapter(APIAdapter):
    endpoint: ClassVar[str] = "/chat/completions"

    def build_payload(
        self,
        model_name: str,
        converted_messages: list[dict[str, Any]],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model_name,
            "messages": converted_messages,
            "temperature": temperature,
        }

        if tools:
            payload["tools"] = [tool.model_dump(exclude_none=True) for tool in tools]
        if tool_choice:
            payload["tool_choice"] = (
                tool_choice
                if isinstance(tool_choice, str)
                else tool_choice.model_dump()
            )
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)

        return payload

    def build_headers(self, api_key: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def prepare_request(
        self,
        *,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        enable_streaming: bool,
        provider: ProviderConfig,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> PreparedRequest:
        converted_messages = [msg.model_dump(exclude_none=True) for msg in messages]

        payload = self.build_payload(
            model_name,
            converted_messages,
            temperature,
            tools,
            max_tokens,
            tool_choice,
            extra_body,
        )

        if enable_streaming:
            payload["stream"] = True
            if provider.name == "mistral":
                payload["stream_options"] = {"stream_tool_calls": True}

        headers = self.build_headers(api_key)

        body = json.dumps(payload).encode("utf-8")

        return PreparedRequest(self.endpoint, headers, body)

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        if data.get("choices"):
            if "message" in data["choices"][0]:
                message = LLMMessage.model_validate(data["choices"][0]["message"])
            elif "delta" in data["choices"][0]:
                message = LLMMessage.model_validate(data["choices"][0]["delta"])
            else:
                raise ValueError("Invalid response data")
            finish_reason = data["choices"][0]["finish_reason"]

        elif "message" in data:
            message = LLMMessage.model_validate(data["message"])
            finish_reason = data["finish_reason"]
        elif "delta" in data:
            message = LLMMessage.model_validate(data["delta"])
            finish_reason = None
        else:
            message = LLMMessage(role=Role.assistant, content="")
            finish_reason = None

        usage_data = data.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
        )

        return LLMChunk(message=message, usage=usage, finish_reason=finish_reason)


@register_adapter(BACKEND_ADAPTERS, "anthropic")
class AnthropicAdapter(APIAdapter):
    """Adapter for Anthropic's native /messages API."""

    endpoint: ClassVar[str] = "/messages"

    def build_payload(
        self,
        model_name: str,
        converted_messages: list[dict[str, Any]],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Convert OpenAI-style messages to Anthropic format
        system_content = ""
        anthropic_messages = []

        for msg in converted_messages:
            role = msg.get("role", "")
            if role == "system":
                content = msg.get("content", "")
                if isinstance(content, str):
                    system_content += content + "\n"
            elif role in ("user", "assistant"):
                content = msg.get("content", "")
                # Handle tool_calls in assistant messages
                if role == "assistant" and msg.get("tool_calls"):
                    # Convert tool calls to Anthropic format
                    anthropic_content = []
                    if content:
                        anthropic_content.append({"type": "text", "text": content})
                    for tc in msg["tool_calls"]:
                        func = tc.get("function", {})
                        input_data = func.get("arguments", "{}")
                        if isinstance(input_data, str):
                            try:
                                input_data = json.loads(input_data)
                            except json.JSONDecodeError:
                                input_data = {}
                        anthropic_content.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": input_data,
                        })
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": anthropic_content,
                    })
                else:
                    # Use content block array format
                    anthropic_messages.append({
                        "role": role,
                        "content": [{"type": "text", "text": content}]
                        if content
                        else [{"type": "text", "text": ""}],
                    })
            elif role == "tool":
                # Tool results in Anthropic format go in a user message
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }
                # Check if last message is user, if so append to it
                if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                    last_content = anthropic_messages[-1]["content"]
                    if isinstance(last_content, list):
                        last_content.append(tool_result)
                    else:
                        anthropic_messages[-1]["content"] = [
                            {"type": "text", "text": last_content} if last_content else None,
                            tool_result,
                        ]
                        anthropic_messages[-1]["content"] = [
                            c for c in anthropic_messages[-1]["content"] if c
                        ]
                else:
                    anthropic_messages.append({
                        "role": "user",
                        "content": [tool_result],
                    })

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 8192,
        }

        if system_content.strip():
            # System must be array of text blocks
            payload["system"] = [{"type": "text", "text": system_content.strip()}]

        if temperature > 0:
            payload["temperature"] = temperature

        if tools:
            payload["tools"] = [self._convert_tool(t) for t in tools]

        if tool_choice:
            if isinstance(tool_choice, str):
                if tool_choice == "auto":
                    payload["tool_choice"] = {"type": "auto"}
                elif tool_choice == "required":
                    payload["tool_choice"] = {"type": "any"}
                elif tool_choice == "none":
                    pass  # Don't send tool_choice
            else:
                payload["tool_choice"] = {
                    "type": "tool",
                    "name": tool_choice.function.name,
                }

        if extra_body:
            payload.update(extra_body)

        return payload

    def _convert_tool(self, tool: AvailableTool) -> dict[str, Any]:
        """Convert OpenAI tool format to Anthropic format."""
        return {
            "name": tool.function.name,
            "description": tool.function.description or "",
            "input_schema": tool.function.parameters
            or {"type": "object", "properties": {}},
        }

    def build_headers(
        self, api_key: str | None = None, is_oauth: bool = False
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            if is_oauth:
                headers["Authorization"] = f"Bearer {api_key}"
                headers["anthropic-beta"] = "oauth-2025-04-20"
            else:
                headers["x-api-key"] = api_key
        return headers

    def prepare_request(
        self,
        *,
        model_name: str,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        enable_streaming: bool,
        provider: ProviderConfig,
        api_key: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> PreparedRequest:
        # Check if OAuth is being used - need to inject Claude Code prefix
        is_oauth = provider.oauth_token is not None
        messages_to_send = list(messages)

        if is_oauth:
            # Claude Code OAuth requires this specific system prompt prefix
            claude_code_prefix = (
                "You are Claude Code, Anthropic's official CLI for Claude."
            )
            # Prepend the required prefix to identify as Claude Code
            prefix_msg = LLMMessage(role=Role.system, content=claude_code_prefix)
            messages_to_send = [prefix_msg, *messages_to_send]

        converted_messages = [
            msg.model_dump(exclude_none=True) for msg in messages_to_send
        ]

        payload = self.build_payload(
            model_name,
            converted_messages,
            temperature,
            tools,
            max_tokens,
            tool_choice,
            extra_body,
        )

        if enable_streaming:
            payload["stream"] = True

        headers = self.build_headers(api_key, is_oauth=is_oauth)

        body = json.dumps(payload).encode("utf-8")
        return PreparedRequest(self.endpoint, headers, body)

    def parse_response(self, data: dict[str, Any]) -> LLMChunk:
        # Handle streaming events
        event_type = data.get("type", "")

        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            text = delta.get("text", "")
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=text),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )

        if event_type == "message_delta":
            usage_data = data.get("usage", {})
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=""),
                usage=LLMUsage(
                    prompt_tokens=0,
                    completion_tokens=usage_data.get("output_tokens", 0),
                ),
                finish_reason=data.get("delta", {}).get("stop_reason"),
            )

        if event_type in (
            "message_start",
            "content_block_start",
            "content_block_stop",
            "message_stop",
            "ping",
        ):
            # These are metadata events, return empty chunk
            return LLMChunk(
                message=LLMMessage(role=Role.assistant, content=""),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )

        # Handle non-streaming response (full message)
        content_blocks = data.get("content", [])
        text_content = ""
        tool_calls = []

        for block in content_blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                text_content += block.get("text", "")
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        message = LLMMessage(
            role=Role.assistant,
            content=text_content if text_content else None,
            tool_calls=tool_calls if tool_calls else None,
        )

        usage_data = data.get("usage", {})
        usage = LLMUsage(
            prompt_tokens=usage_data.get("input_tokens", 0),
            completion_tokens=usage_data.get("output_tokens", 0),
        )

        return LLMChunk(
            message=message,
            usage=usage,
            finish_reason=data.get("stop_reason"),
        )


class GenericBackend:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        provider: ProviderConfig,
        timeout: float = 720.0,
    ) -> None:
        """Initialize the backend.

        Args:
            client: Optional httpx client to use. If not provided, one will be created.
        """
        self._client = client
        self._owns_client = client is None
        self._provider = provider
        self._timeout = timeout

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

    async def _get_auth_info(self) -> tuple[str | None, dict[str, str]]:
        """Get API key and extra headers, handling OAuth tokens.

        Returns:
            Tuple of (api_key, extra_headers) where api_key may be a Bearer token
            for OAuth authentication.
        """
        extra_headers: dict[str, str] = {}

        # Check for OAuth token first
        if self._provider.oauth_token:
            # Refresh token if expired
            if self._provider.oauth_token.is_expired():
                await self._refresh_oauth_token()

            if self._provider.oauth_token and not self._provider.oauth_token.is_expired():
                # OAuth token is valid - use Bearer auth
                api_key = self._provider.oauth_token.access_token
                # Add required Anthropic OAuth headers
                extra_headers["anthropic-version"] = "2023-06-01"
                extra_headers["anthropic-beta"] = "oauth-2025-04-20"
                return api_key, extra_headers

        # Fall back to API key from env var
        api_key = (
            os.getenv(self._provider.api_key_env_var)
            if self._provider.api_key_env_var
            else None
        )
        return api_key, extra_headers

    async def _refresh_oauth_token(self) -> None:
        """Refresh the OAuth token if it's expired."""
        if not self._provider.oauth_token:
            return

        try:
            from vibe.core.config import save_oauth_token
            from vibe.core.oauth.claude import refresh_token

            new_token = await refresh_token(self._provider.oauth_token.refresh_token)
            # Update in-memory token
            self._provider.oauth_token = new_token
            # Persist to config
            save_oauth_token(self._provider.name, new_token)
        except Exception:
            # If refresh fails, clear the token so we fall back to API key
            pass

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
    ) -> LLMChunk:
        api_key, oauth_headers = await self._get_auth_info()

        api_style = getattr(self._provider, "api_style", "openai")
        adapter = BACKEND_ADAPTERS[api_style]

        extra_body = getattr(model, "extra_body", None) or None

        endpoint, headers, body = adapter.prepare_request(
            model_name=model.name,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            enable_streaming=False,
            provider=self._provider,
            api_key=api_key,
            extra_body=extra_body,
        )

        # Apply OAuth headers first, then extra_headers can override
        headers.update(oauth_headers)
        if extra_headers:
            headers.update(extra_headers)

        url = f"{self._provider.api_base}{endpoint}"

        try:
            res_data, _ = await self._make_request(url, body, headers)
            return adapter.parse_response(res_data)

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
    ) -> AsyncGenerator[LLMChunk, None]:
        api_key, oauth_headers = await self._get_auth_info()

        api_style = getattr(self._provider, "api_style", "openai")
        adapter = BACKEND_ADAPTERS[api_style]

        extra_body = getattr(model, "extra_body", None) or None

        endpoint, headers, body = adapter.prepare_request(
            model_name=model.name,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            enable_streaming=True,
            provider=self._provider,
            api_key=api_key,
            extra_body=extra_body,
        )

        # Apply OAuth headers first, then extra_headers can override
        headers.update(oauth_headers)
        if extra_headers:
            headers.update(extra_headers)

        url = f"{self._provider.api_base}{endpoint}"

        try:
            async for res_data in self._make_streaming_request(url, body, headers):
                yield adapter.parse_response(res_data)

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

    @async_retry(tries=3)
    async def _make_request(
        self, url: str, data: bytes, headers: dict[str, str]
    ) -> HTTPResponse:
        client = self._get_client()
        response = await client.post(url, content=data, headers=headers)
        response.raise_for_status()

        response_headers = dict(response.headers.items())
        response_body = response.json()
        return self.HTTPResponse(response_body, response_headers)

    @async_generator_retry(tries=3)
    async def _make_streaming_request(
        self, url: str, data: bytes, headers: dict[str, str]
    ) -> AsyncGenerator[dict[str, Any]]:
        client = self._get_client()
        async with client.stream(
            method="POST", url=url, content=data, headers=headers
        ) as response:
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
