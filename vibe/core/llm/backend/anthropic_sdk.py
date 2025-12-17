from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import os
import types
from typing import TYPE_CHECKING

import anthropic
import httpx

from vibe.core.llm.exceptions import BackendErrorBuilder
from vibe.core.types import (
    AvailableTool,
    FunctionCall,
    LLMChunk,
    LLMMessage,
    LLMUsage,
    Role,
    StrToolChoice,
    ToolCall,
)

if TYPE_CHECKING:
    from vibe.core.config import ModelConfig, ProviderConfig


# Required system prompt prefix for Claude Code OAuth
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


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
            if msg.role == Role.system:
                if msg.content:
                    system_parts.append(msg.content)
            elif msg.role == Role.user:
                anthropic_messages.append({
                    "role": "user",
                    "content": msg.content or "",
                })
            elif msg.role == Role.assistant:
                if msg.tool_calls or msg.reasoning_content:
                    # Assistant message with tool calls or thinking content
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
                        for tc in msg.tool_calls:
                            input_data = tc.function.arguments or "{}"
                            if isinstance(input_data, str):
                                try:
                                    input_data = json.loads(input_data)
                                except json.JSONDecodeError:
                                    input_data = {}
                            content.append({
                                "type": "tool_use",
                                "id": tc.id or "",
                                "name": tc.function.name or "",
                                "input": input_data,
                            })
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": content,
                    })
                else:
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                    })
            elif msg.role == Role.tool:
                # Tool results go in a user message
                tool_result: anthropic.types.ToolResultBlockParam = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id or "",
                    "content": msg.content or "",
                }
                # Append to last user message or create new one
                if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                    last_content = anthropic_messages[-1]["content"]
                    if isinstance(last_content, list):
                        last_content.append(tool_result)  # type: ignore[arg-type]
                    else:
                        anthropic_messages[-1]["content"] = [  # type: ignore[typeddict-item]
                            {"type": "text", "text": last_content} if last_content else {"type": "text", "text": ""},
                            tool_result,
                        ]
                else:
                    anthropic_messages.append({
                        "role": "user",
                        "content": [tool_result],
                    })

        return system_parts, anthropic_messages

    def add_cache_control_to_messages(
        self, messages: list[anthropic.types.MessageParam]
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
        if len(messages) < 2:
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
                        "cache_control": {"type": "ephemeral"},
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
                    block["cache_control"] = {"type": "ephemeral"}
                    content[i] = block  # type: ignore[call-overload]
                    cache_added = True
                    break
                elif block_type == "text" and block.get("text"):
                    # Non-empty text block supports cache_control
                    block["cache_control"] = {"type": "ephemeral"}
                    content[i] = block  # type: ignore[call-overload]
                    cache_added = True
                    break
                # Skip tool_use, thinking, and empty text blocks

            if cache_added:
                messages[target_idx] = {
                    "role": target_msg["role"],
                    "content": content,
                }

        return messages

    def prepare_system_blocks(
        self, system_parts: list[str], enable_caching: bool = False
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
                block["cache_control"] = {"type": "ephemeral"}  # type: ignore[typeddict-item]
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
                            name=block.name,
                            arguments=json.dumps(block.input),
                        ),
                    )
                )
        return tool_calls

    def parse_text_content(
        self, content: list[anthropic.types.ContentBlock]
    ) -> str:
        """Extract text content from content blocks."""
        text_parts = []
        for block in content:
            if block.type == "text":
                text_parts.append(block.text)
        return "".join(text_parts)


class AnthropicBackend:
    """Backend using the official Anthropic Python SDK."""

    def __init__(self, provider: ProviderConfig, timeout: float = 720.0) -> None:
        self._client: anthropic.AsyncAnthropic | None = None
        self._provider = provider
        self._mapper = AnthropicMapper()
        self._timeout = timeout

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
            self._provider.oauth_token = None

    async def _ensure_fresh_token(self) -> None:
        """Ensure OAuth token is refreshed if expired.

        This should be called at the start of both complete() and complete_streaming()
        to ensure the token is valid before making API calls.
        """
        if self._provider.oauth_token and self._provider.oauth_token.is_expired():
            await self._refresh_oauth_token()

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
    ) -> LLMChunk:
        await self._ensure_fresh_token()

        try:
            system_parts, anthropic_messages = self._mapper.prepare_messages(
                messages, is_oauth=self._is_oauth()
            )

            # Enable prompt caching for system prompt and conversation history
            anthropic_messages = self._mapper.add_cache_control_to_messages(
                anthropic_messages
            )

            kwargs: dict = {
                "model": model.name,
                "messages": anthropic_messages,
                "max_tokens": max_tokens or 8192,
            }

            if system_parts:
                kwargs["system"] = self._mapper.prepare_system_blocks(
                    system_parts, enable_caching=True
                )
            if temperature > 0:
                kwargs["temperature"] = temperature
            if tools:
                kwargs["tools"] = self._mapper.prepare_tools(tools)
            if tool_choice:
                tc = self._mapper.prepare_tool_choice(tool_choice)
                if tc:
                    kwargs["tool_choice"] = tc

            # Handle thinking mode from model.extra_body
            extra_call_headers: dict[str, str] = {}
            thinking_enabled = (
                model.extra_body
                and "thinking" in model.extra_body
                and self._mapper.is_compatible_with_thinking(messages)
            )
            if thinking_enabled:
                kwargs["thinking"] = model.extra_body["thinking"]  # type: ignore

                if self._is_oauth():
                    extra_call_headers["anthropic-beta"] = (
                        "oauth-2025-04-20,interleaved-thinking-2025-05-14"
                    )
                else:
                    extra_call_headers["anthropic-beta"] = (
                        "interleaved-thinking-2025-05-14"
                    )
                # Temperature must be 1 for thinking mode
                if "temperature" in kwargs:
                    del kwargs["temperature"]

            response = await self._get_client().messages.create(
                **kwargs,
                extra_headers=extra_call_headers if extra_call_headers else None,
            )

            text_content = self._mapper.parse_text_content(response.content)
            tool_calls = self._mapper.parse_tool_calls(response.content)

            return LLMChunk(
                message=LLMMessage(
                    role=Role.assistant,
                    content=text_content or None,
                    tool_calls=tool_calls if tool_calls else None,
                ),
                usage=LLMUsage(
                    prompt_tokens=response.usage.input_tokens,
                    completion_tokens=response.usage.output_tokens,
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
    ) -> AsyncGenerator[LLMChunk, None]:
        await self._ensure_fresh_token()

        try:
            system_parts, anthropic_messages = self._mapper.prepare_messages(
                messages, is_oauth=self._is_oauth()
            )

            # Enable prompt caching for system prompt and conversation history
            anthropic_messages = self._mapper.add_cache_control_to_messages(
                anthropic_messages
            )

            kwargs: dict = {
                "model": model.name,
                "messages": anthropic_messages,
                "max_tokens": max_tokens or 8192,
            }

            if system_parts:
                kwargs["system"] = self._mapper.prepare_system_blocks(
                    system_parts, enable_caching=True
                )
            if temperature > 0:
                kwargs["temperature"] = temperature
            if tools:
                kwargs["tools"] = self._mapper.prepare_tools(tools)
            if tool_choice:
                tc = self._mapper.prepare_tool_choice(tool_choice)
                if tc:
                    kwargs["tool_choice"] = tc

            # Handle thinking mode from model.extra_body
            extra_call_headers: dict[str, str] = {}
            thinking_enabled = (
                model.extra_body
                and "thinking" in model.extra_body
                and self._mapper.is_compatible_with_thinking(messages)
            )
            if thinking_enabled:
                kwargs["thinking"] = model.extra_body["thinking"]  # type: ignore
                if self._is_oauth():
                    extra_call_headers["anthropic-beta"] = (
                        "oauth-2025-04-20,interleaved-thinking-2025-05-14"
                    )
                else:
                    extra_call_headers["anthropic-beta"] = (
                        "interleaved-thinking-2025-05-14"
                    )
                # Temperature must be 1 for thinking mode
                if "temperature" in kwargs:
                    del kwargs["temperature"]

            # Accumulate tool calls across stream
            current_tool_calls: dict[int, dict] = {}
            total_input_tokens = 0
            total_output_tokens = 0

            async with self._get_client().messages.stream(
                **kwargs,
                extra_headers=extra_call_headers if extra_call_headers else None,
            ) as stream:
                async for event in stream:
                    if event.type == "message_start":
                        if event.message.usage:
                            total_input_tokens = event.message.usage.input_tokens
                    elif event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            current_tool_calls[event.index] = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "input": "",
                            }
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield LLMChunk(
                                message=LLMMessage(
                                    role=Role.assistant,
                                    content=event.delta.text,
                                ),
                                usage=LLMUsage(
                                    prompt_tokens=0,
                                    completion_tokens=0,
                                ),
                                finish_reason=None,
                            )
                        elif event.delta.type == "thinking_delta":
                            # Yield thinking/reasoning content
                            yield LLMChunk(
                                message=LLMMessage(
                                    role=Role.assistant,
                                    reasoning_content=event.delta.thinking,
                                ),
                                usage=LLMUsage(
                                    prompt_tokens=0,
                                    completion_tokens=0,
                                ),
                                finish_reason=None,
                            )
                        elif event.delta.type == "signature_delta":
                            # Yield thinking signature (required for API)
                            yield LLMChunk(
                                message=LLMMessage(
                                    role=Role.assistant,
                                    thinking_signature=event.delta.signature,
                                ),
                                usage=LLMUsage(
                                    prompt_tokens=0,
                                    completion_tokens=0,
                                ),
                                finish_reason=None,
                            )
                        elif event.delta.type == "input_json_delta":
                            if event.index in current_tool_calls:
                                current_tool_calls[event.index]["input"] += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        # When a tool use block completes, yield the tool call
                        if event.index in current_tool_calls:
                            tc_data = current_tool_calls[event.index]
                            yield LLMChunk(
                                message=LLMMessage(
                                    role=Role.assistant,
                                    content=None,
                                    tool_calls=[
                                        ToolCall(
                                            id=tc_data["id"],
                                            index=event.index,  # Required by agent
                                            function=FunctionCall(
                                                name=tc_data["name"],
                                                arguments=tc_data["input"],
                                            ),
                                        )
                                    ],
                                ),
                                usage=LLMUsage(
                                    prompt_tokens=0,
                                    completion_tokens=0,
                                ),
                                finish_reason=None,
                            )
                            del current_tool_calls[event.index]
                    elif event.type == "message_delta":
                        if event.usage:
                            total_output_tokens = event.usage.output_tokens
                        yield LLMChunk(
                            message=LLMMessage(
                                role=Role.assistant,
                                content="",
                            ),
                            usage=LLMUsage(
                                prompt_tokens=total_input_tokens,
                                completion_tokens=total_output_tokens,
                            ),
                            finish_reason=event.delta.stop_reason,
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
