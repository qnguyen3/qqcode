from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
import json
import logging
import os
import types
from typing import TYPE_CHECKING, Any, cast

import anthropic
import httpx

from vibe.core.config import (
    DEFAULT_BUDGETS,
    ThinkingLevel,
    resolve_thinking_level,
    save_oauth_token,
)
from vibe.core.images import resolve_messages
from vibe.core.llm.cache import CachePlan
from vibe.core.llm.capabilities import clamp_max_tokens
from vibe.core.llm.exceptions import BackendErrorBuilder
from vibe.core.oauth.registry import get_flow
from vibe.core.types import (
    AvailableTool,
    FunctionCall,
    ImagePart,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
    ToolCall,
)

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig
    from vibe.core.oauth.token import OAuthToken

logger = logging.getLogger(__name__)


# Required system prompt prefix for Claude Code OAuth
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

# Cache control needs a previous turn to attach to.
MIN_MESSAGES_FOR_CACHE = 2


@dataclass
class _StreamState:
    """Token totals and in-flight tool calls for a streamed response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


def _delta_chunks(event: Any, state: _StreamState) -> Iterator[LLMChunk]:
    delta = event.delta
    match delta.type:
        case "text_delta":
            yield LLMChunk(
                message=LLMMessage(role=Role.assistant, content=delta.text),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )
        case "thinking_delta":
            yield LLMChunk(
                message=LLMMessage(
                    role=Role.assistant, reasoning_content=delta.thinking
                ),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )
        case "signature_delta":
            yield LLMChunk(
                message=LLMMessage(
                    role=Role.assistant, thinking_signature=delta.signature
                ),
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
                finish_reason=None,
            )
        case "input_json_delta":
            if (tool_call := state.tool_calls.get(event.index)) is not None:
                tool_call["input"] += delta.partial_json


def _tool_call_chunk(index: int, data: dict[str, str]) -> LLMChunk:
    return LLMChunk(
        message=LLMMessage(
            role=Role.assistant,
            tool_calls=[
                ToolCall(
                    id=data["id"],
                    index=index,  # Required by agent
                    function=FunctionCall(name=data["name"], arguments=data["input"]),
                )
            ],
        ),
        usage=LLMUsage(prompt_tokens=0, completion_tokens=0),
        finish_reason=None,
    )


class AnthropicMapper:
    """Convert Vibe types to/from Anthropic SDK types."""

    def is_compatible_with_thinking(self, messages: list[LLMMessage]) -> bool:
        """Check if message history is compatible with thinking mode.

        Returns False if there are assistant messages with tool_calls but no
        thinking blocks - this would cause API errors when thinking is enabled.
        """
        for msg in messages:
            if msg.role == Role.assistant and msg.tool_calls:
                # If assistant message has tool_calls but no thinking block,
                # it's incompatible with thinking mode
                if not (msg.reasoning_content and msg.thinking_signature):
                    return False
        return True

    def prepare_messages(
        self, messages: list[LLMMessage], is_oauth: bool
    ) -> tuple[list[str], list[anthropic.types.MessageParam]]:
        """Convert messages to Anthropic format, extracting system prompt.

        Returns system parts as a list (not joined) so each can be a separate
        text block - this is required for Claude Code OAuth validation.
        """
        system_parts: list[str] = []
        anthropic_messages: list[anthropic.types.MessageParam] = []

        # Prepend Claude Code prefix for OAuth - MUST be first text block
        if is_oauth:
            system_parts.append(CLAUDE_CODE_SYSTEM_PREFIX)

        for msg in messages:
            match msg.role:
                case Role.system:
                    if msg.content:
                        system_parts.append(msg.content)
                case Role.user:
                    anthropic_messages.append(self._convert_user_message(msg))
                case Role.assistant:
                    anthropic_messages.append(self._convert_assistant_message(msg))
                case Role.tool:
                    self._append_tool_result(
                        anthropic_messages,
                        anthropic.types.ToolResultBlockParam(
                            type="tool_result",
                            tool_use_id=msg.tool_call_id or "",
                            content=msg.content or "",
                        ),
                    )

        return system_parts, anthropic_messages

    def _convert_user_message(self, msg: LLMMessage) -> anthropic.types.MessageParam:
        if not msg.images:
            return {"role": "user", "content": msg.content or ""}

        content: list[anthropic.types.ContentBlockParam] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        content.extend(self._convert_image(image) for image in msg.images)
        return {"role": "user", "content": content}

    @staticmethod
    def _convert_image(image: ImagePart) -> anthropic.types.ContentBlockParam:
        block: anthropic.types.ImageBlockParam = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": cast(Any, image.media_type),
                "data": image.data,
            },
        }
        return block

    def _convert_assistant_message(
        self, msg: LLMMessage
    ) -> anthropic.types.MessageParam:
        if not msg.tool_calls and not msg.reasoning_content:
            return {"role": "assistant", "content": msg.content or ""}

        content: list[anthropic.types.ContentBlockParam] = []
        # Thinking block must come first (required by Anthropic API)
        if msg.reasoning_content and msg.thinking_signature:
            content.append({
                "type": "thinking",
                "thinking": msg.reasoning_content,
                "signature": msg.thinking_signature,
            })  # type: ignore[typeddict-item]
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        if msg.tool_calls:
            content.extend(
                self._convert_tool_use(tool_call) for tool_call in msg.tool_calls
            )
        return {"role": "assistant", "content": content}

    @staticmethod
    def _convert_tool_use(tool_call: ToolCall) -> anthropic.types.ContentBlockParam:
        input_data = tool_call.function.arguments or "{}"
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                input_data = {}
        return {
            "type": "tool_use",
            "id": tool_call.id or "",
            "name": tool_call.function.name or "",
            "input": input_data,
        }

    @staticmethod
    def _append_tool_result(
        anthropic_messages: list[anthropic.types.MessageParam],
        tool_result: anthropic.types.ToolResultBlockParam,
    ) -> None:
        """Append a tool result to the last user message, or start a new one."""
        last = anthropic_messages[-1] if anthropic_messages else None
        if last is None or last["role"] != "user":
            anthropic_messages.append({"role": "user", "content": [tool_result]})
            return

        last_content = last["content"]
        if isinstance(last_content, str):
            text_block: anthropic.types.TextBlockParam = {
                "type": "text",
                "text": last_content,
            }
            last["content"] = [text_block, tool_result]
            return

        blocks = list(last_content)
        blocks.append(tool_result)
        last["content"] = blocks

    @staticmethod
    def _cache_control(ttl: str | None) -> anthropic.types.CacheControlEphemeralParam:
        control: anthropic.types.CacheControlEphemeralParam = {"type": "ephemeral"}
        if ttl == "1h":
            control["ttl"] = "1h"
        return control

    def add_cache_control_to_messages(
        self, messages: list[anthropic.types.MessageParam], ttl: str | None = None
    ) -> list[anthropic.types.MessageParam]:
        """Add cache_control to the last content block of the second-to-last message.

        This enables caching of the conversation history up to the current turn.
        We cache the second-to-last message (not the last) because the last message
        is typically the current user input which may change.

        For agentic workflows:
        - Turn 1: [User1] -> cache at User1
        - Turn 2: [User1+cache_hit] [Asst1] [User2] -> cache at Asst1, User1 is cache hit
        - Turn 3: [User1+Asst1:cache_hit] [User2] [Asst2] [User3] -> cache at User2
        """
        if len(messages) < MIN_MESSAGES_FOR_CACHE:
            # Need at least 2 messages to cache history
            return messages

        # Work on a copy to avoid mutating the original
        messages = list(messages)

        # Target the second-to-last message for cache control
        target_idx = len(messages) - 2
        target_msg = messages[target_idx]
        content = target_msg["content"]

        if isinstance(content, str):
            # Convert string content to list with cache control
            messages[target_idx] = {
                "role": target_msg["role"],
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": self._cache_control(ttl),
                    }
                ],
            }
        elif isinstance(content, list) and len(content) > 0:
            # Find a suitable block to add cache_control to
            # Must be text (non-empty) or tool_result
            content = list(content)  # Copy the list

            # Search backwards for a cacheable block
            cache_added = False
            for i in range(len(content) - 1, -1, -1):
                block = dict(content[i])  # type: ignore[arg-type]
                block_type = block.get("type", "")

                if block_type == "tool_result":
                    # tool_result supports cache_control
                    block["cache_control"] = self._cache_control(ttl)
                    content[i] = block  # type: ignore[call-overload]
                    cache_added = True
                    break
                elif block_type == "text" and block.get("text"):
                    # Non-empty text block supports cache_control
                    block["cache_control"] = self._cache_control(ttl)
                    content[i] = block  # type: ignore[call-overload]
                    cache_added = True
                    break
                # Skip tool_use, thinking, and empty text blocks

            if cache_added:
                messages[target_idx] = {"role": target_msg["role"], "content": content}

        return messages

    def prepare_system_blocks(
        self,
        system_parts: list[str],
        enable_caching: bool = False,
        ttl: str | None = None,
    ) -> list[anthropic.types.TextBlockParam]:
        """Convert system parts to text blocks for the system field.

        If enable_caching is True, adds cache_control to the last block
        to enable Anthropic's prompt caching for the system prompt.
        """
        blocks: list[anthropic.types.TextBlockParam] = []
        for i, part in enumerate(system_parts):
            block: anthropic.types.TextBlockParam = {"type": "text", "text": part}
            # Add cache control to the last system block
            if enable_caching and i == len(system_parts) - 1:
                block["cache_control"] = self._cache_control(ttl)  # type: ignore[typeddict-item]
            blocks.append(block)
        return blocks

    def prepare_tools(
        self, tools: list[AvailableTool]
    ) -> list[anthropic.types.ToolParam]:
        """Convert tools to Anthropic format."""
        return [
            {
                "name": tool.function.name,
                "description": tool.function.description or "",
                "input_schema": tool.function.parameters
                or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]

    def prepare_tool_choice(
        self, tool_choice: StrToolChoice | AvailableTool
    ) -> anthropic.types.message_create_params.ToolChoice | None:
        """Convert tool choice to Anthropic format."""
        if isinstance(tool_choice, str):
            if tool_choice == "auto":
                return {"type": "auto"}
            elif tool_choice == "required":
                return {"type": "any"}
            elif tool_choice == "none":
                return None
        else:
            return {"type": "tool", "name": tool_choice.function.name}
        return None

    def parse_tool_calls(
        self, content: list[anthropic.types.ContentBlock]
    ) -> list[ToolCall]:
        """Extract tool calls from content blocks."""
        tool_calls = []
        for block in content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        function=FunctionCall(
                            name=block.name, arguments=json.dumps(block.input)
                        ),
                    )
                )
        return tool_calls

    def parse_text_content(self, content: list[anthropic.types.ContentBlock]) -> str:
        """Extract text content from content blocks."""
        text_parts = []
        for block in content:
            if block.type == "text":
                text_parts.append(block.text)
        return "".join(text_parts)


class AnthropicBackend:
    """Backend using the official Anthropic Python SDK."""

    def __init__(
        self,
        provider: ProviderConfig,
        timeout: float = 720.0,
        honor_retry_after: bool = True,
    ) -> None:
        self._client: anthropic.AsyncAnthropic | None = None
        self._provider = provider
        self._mapper = AnthropicMapper()
        self._timeout = timeout
        self._honor_retry_after = honor_retry_after
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def _is_oauth(self) -> bool:
        """Check if OAuth token is configured."""
        return self._provider.oauth_token is not None

    def _get_api_key(self) -> str | None:
        """Get API key from OAuth token or environment."""
        if self._is_oauth():
            return self._provider.oauth_token.access_token  # type: ignore
        if self._provider.api_key_env_var:
            return os.getenv(self._provider.api_key_env_var)
        return None

    async def _refresh_oauth_token(self, token: OAuthToken | None = None) -> None:
        """Refresh the OAuth token if it's expired."""
        token = token or self._provider.oauth_token
        if token is None:
            return

        try:
            flow = get_flow(self._provider.oauth_flow or self._provider.name)
            new_token = await flow.refresh(token)
            # Update in-memory token
            self._provider.oauth_token = new_token
            # Persist to config
            save_oauth_token(self._provider.name, new_token)
        except Exception:
            # If refresh fails, clear the token so we fall back to API key
            logger.debug(
                "OAuth refresh failed for provider %s",
                self._provider.name,
                exc_info=True,
            )
            self._provider.oauth_token = None

    async def _ensure_fresh_token(self) -> None:
        """Ensure OAuth token is refreshed if expired.

        This should be called at the start of both complete() and complete_streaming()
        to ensure the token is valid before making API calls. Concurrent callers
        share a per-provider lock so the token is refreshed at most once.
        """
        token = self._provider.oauth_token
        if token is None or not token.is_expired(skew=60):
            return

        lock = self._refresh_locks.setdefault(self._provider.name, asyncio.Lock())
        async with lock:
            token = self._provider.oauth_token
            if token is None or not token.is_expired(skew=60):
                return
            await self._refresh_oauth_token(token)

    def _apply_thinking(
        self,
        kwargs: dict,
        messages: list[LLMMessage],
        model: ModelConfig,
        thinking_level: ThinkingLevel | None,
    ) -> dict[str, str]:
        """Add thinking config to request kwargs and return extra headers."""
        extra_body = model.extra_body or {}
        level = resolve_thinking_level(model, thinking_level)
        thinking: dict | None = None
        if "thinking" in extra_body:
            candidate = extra_body["thinking"]
            thinking = candidate if isinstance(candidate, dict) else None
        elif level is not None and level is not ThinkingLevel.OFF:
            thinking = {"type": "enabled", "budget_tokens": DEFAULT_BUDGETS[level]}

        if not thinking or not self._mapper.is_compatible_with_thinking(messages):
            return {}

        kwargs["thinking"] = thinking
        # Temperature must be 1 for thinking mode
        kwargs.pop("temperature", None)

        if self._is_oauth():
            return {
                "anthropic-beta": "oauth-2025-04-20,interleaved-thinking-2025-05-14"
            }
        return {"anthropic-beta": "interleaved-thinking-2025-05-14"}

    def _build_client(self) -> anthropic.AsyncAnthropic:
        """Build the Anthropic client with proper authentication."""
        api_key = self._get_api_key()

        if self._is_oauth():
            # For OAuth, clear env var and use auth_token for Bearer auth
            if "ANTHROPIC_API_KEY" in os.environ:
                del os.environ["ANTHROPIC_API_KEY"]

            # Use auth_token for Bearer token authentication (OAuth)
            return anthropic.AsyncAnthropic(
                auth_token=api_key,  # Uses Authorization: Bearer header
                base_url=self._provider.api_base.rstrip("/"),
                timeout=self._timeout,
                default_headers={
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            )
        else:
            # Standard API key authentication
            return anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url=self._provider.api_base.rstrip("/"),
                timeout=self._timeout,
            )

    async def __aenter__(self) -> AnthropicBackend:
        self._client = self._build_client()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.close()

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> LLMChunk:
        await self._ensure_fresh_token()

        if not model.capabilities.tools:
            tools = None
            tool_choice = None
        elif not model.capabilities.tool_choice:
            tool_choice = None

        plan = cache_plan if cache_plan is not None else CachePlan(enabled=True)

        try:
            kwargs = self._build_request_kwargs(
                model=model,
                messages=messages,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                plan=plan,
            )

            # Handle thinking mode from the session level or model.extra_body
            extra_call_headers = self._apply_thinking(
                kwargs, messages, model, thinking_level
            )

            response = await self._get_client().messages.create(
                **kwargs,
                extra_headers=extra_call_headers if extra_call_headers else None,
            )

            text_content = self._mapper.parse_text_content(response.content)
            tool_calls = self._mapper.parse_tool_calls(response.content)
            cache_read_tokens = response.usage.cache_read_input_tokens or 0
            cache_write_tokens = response.usage.cache_creation_input_tokens or 0

            return LLMChunk(
                message=LLMMessage(
                    role=Role.assistant,
                    content=text_content or None,
                    tool_calls=tool_calls if tool_calls else None,
                ),
                usage=LLMUsage(
                    prompt_tokens=(
                        response.usage.input_tokens
                        + cache_read_tokens
                        + cache_write_tokens
                    ),
                    completion_tokens=response.usage.output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                ),
                finish_reason=response.stop_reason,
            )

        except anthropic.APIStatusError as e:
            raise BackendErrorBuilder.build_http_error(
                provider=self._provider.name,
                endpoint=self._provider.api_base,
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
                endpoint=self._provider.api_base,
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
        temperature: float,
        tools: list[AvailableTool] | None,
        max_tokens: int | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        extra_headers: dict[str, str] | None,
        cache_plan: CachePlan | None = None,
        thinking_level: ThinkingLevel | None = None,
    ) -> AsyncGenerator[LLMChunk, None]:
        await self._ensure_fresh_token()

        if not model.capabilities.tools:
            tools = None
            tool_choice = None
        elif not model.capabilities.tool_choice:
            tool_choice = None

        plan = cache_plan if cache_plan is not None else CachePlan(enabled=True)

        try:
            kwargs = self._build_request_kwargs(
                model=model,
                messages=messages,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                plan=plan,
            )

            # Handle thinking mode from the session level or model.extra_body
            extra_call_headers = self._apply_thinking(
                kwargs, messages, model, thinking_level
            )

            state = _StreamState()
            async with self._get_client().messages.stream(
                **kwargs,
                extra_headers=extra_call_headers if extra_call_headers else None,
            ) as stream:
                async for event in stream:
                    for chunk in self._stream_event_chunks(event, state):
                        yield chunk

        except anthropic.APIStatusError as e:
            raise BackendErrorBuilder.build_http_error(
                provider=self._provider.name,
                endpoint=self._provider.api_base,
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
                endpoint=self._provider.api_base,
                error=e,
                model=model.name,
                messages=messages,
                temperature=temperature,
                has_tools=bool(tools),
                tool_choice=tool_choice,
            ) from e

    def _build_request_kwargs(
        self,
        *,
        model: ModelConfig,
        messages: list[LLMMessage],
        temperature: float,
        tools: list[AvailableTool] | None,
        tool_choice: StrToolChoice | AvailableTool | None,
        max_tokens: int | None,
        plan: CachePlan,
    ) -> dict[str, Any]:
        messages = resolve_messages(messages, vision=model.capabilities.vision)
        system_parts, anthropic_messages = self._mapper.prepare_messages(
            messages, is_oauth=self._is_oauth()
        )

        # Enable prompt caching for system prompt and conversation history
        if plan.enabled:
            anthropic_messages = self._mapper.add_cache_control_to_messages(
                anthropic_messages, ttl=plan.ttl
            )

        kwargs: dict[str, Any] = {
            "model": model.name,
            "messages": anthropic_messages,
            "max_tokens": clamp_max_tokens(model, max_tokens) or 8192,
        }

        if system_parts:
            kwargs["system"] = self._mapper.prepare_system_blocks(
                system_parts, enable_caching=plan.enabled, ttl=plan.ttl
            )
        if temperature > 0:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._mapper.prepare_tools(tools)
        if tool_choice:
            tc = self._mapper.prepare_tool_choice(tool_choice)
            if tc:
                kwargs["tool_choice"] = tc
        return kwargs

    def _stream_event_chunks(
        self, event: Any, state: _StreamState
    ) -> Iterator[LLMChunk]:
        match event.type:
            case "message_start":
                if usage := event.message.usage:
                    state.input_tokens = usage.input_tokens
                    state.cache_read_tokens = usage.cache_read_input_tokens or 0
                    state.cache_write_tokens = usage.cache_creation_input_tokens or 0
            case "content_block_start" if event.content_block.type == "tool_use":
                state.tool_calls[event.index] = {
                    "id": event.content_block.id,
                    "name": event.content_block.name,
                    "input": "",
                }
            case "content_block_delta":
                yield from _delta_chunks(event, state)
            case "content_block_stop":
                if tc_data := state.tool_calls.pop(event.index, None):
                    yield _tool_call_chunk(event.index, tc_data)
            case "message_delta":
                if event.usage:
                    state.output_tokens = event.usage.output_tokens
                yield LLMChunk(
                    message=LLMMessage(role=Role.assistant, content=""),
                    usage=LLMUsage(
                        prompt_tokens=state.prompt_tokens(),
                        completion_tokens=state.output_tokens,
                        cache_read_tokens=state.cache_read_tokens,
                        cache_write_tokens=state.cache_write_tokens,
                    ),
                    finish_reason=event.delta.stop_reason,
                )

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
        result = await self.complete(
            model=model,
            messages=messages,
            temperature=temperature,
            tools=tools,
            max_tokens=1,
            tool_choice=tool_choice,
            extra_headers=extra_headers,
        )
        assert result.usage is not None

        return result.usage.prompt_tokens
