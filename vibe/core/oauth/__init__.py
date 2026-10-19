from __future__ import annotations

from vibe.core.oauth.base import (
    AnyOAuthToken,
    AuthInteraction,
    DeviceCodeInfo,
    OAuthFlow,
    SelectOption,
)
from vibe.core.oauth.callback_server import (
    OAuthCallbackServer,
    start_oauth_callback_server,
    wait_for_callback_or_manual_input,
)
from vibe.core.oauth.claude import (
    ClaudeOAuthFlow,
    exchange_token,
    get_authorize_url,
    get_pkce_challenge,
    refresh_token,
)
from vibe.core.oauth.device_code import (
    DeviceFlowCancelled,
    DeviceFlowError,
    DeviceFlowTimeout,
    PollResult,
    PollStatus,
    poll_device_code_flow,
)
from vibe.core.oauth.github_copilot import GitHubCopilotOAuthFlow
from vibe.core.oauth.kimi_coding import KimiCodingOAuthFlow
from vibe.core.oauth.openai_chatgpt import OpenAIChatGPTOAuthFlow
from vibe.core.oauth.openai_codex import OpenAICodexOAuthFlow
from vibe.core.oauth.openrouter import OpenRouterOAuthFlow
from vibe.core.oauth.pkce import base64_url_decode, base64_url_encode, generate_pkce
from vibe.core.oauth.qwen import (
    DeviceCodeResponse,
    QwenOAuthFlow,
    get_pkce_challenge as qwen_get_pkce_challenge,
    poll_for_token as qwen_poll_for_token,
    refresh_token as qwen_refresh_token,
    request_device_code as qwen_request_device_code,
)
from vibe.core.oauth.radius import RadiusOAuthFlow
from vibe.core.oauth.registry import UnknownOAuthFlowError, get_flow, register_flow
from vibe.core.oauth.token import AnthropicOAuthToken, OAuthToken, QwenOAuthToken
from vibe.core.oauth.xai import XaiOAuthFlow

__all__ = [
    "AnthropicOAuthToken",
    "AnyOAuthToken",
    "AuthInteraction",
    "ClaudeOAuthFlow",
    "DeviceCodeInfo",
    "DeviceCodeResponse",
    "DeviceFlowCancelled",
    "DeviceFlowError",
    "DeviceFlowTimeout",
    "GitHubCopilotOAuthFlow",
    "KimiCodingOAuthFlow",
    "OAuthCallbackServer",
    "OAuthFlow",
    "OAuthToken",
    "OpenAIChatGPTOAuthFlow",
    "OpenAICodexOAuthFlow",
    "OpenRouterOAuthFlow",
    "PollResult",
    "PollStatus",
    "QwenOAuthFlow",
    "QwenOAuthToken",
    "RadiusOAuthFlow",
    "SelectOption",
    "UnknownOAuthFlowError",
    "XaiOAuthFlow",
    "base64_url_decode",
    "base64_url_encode",
    "exchange_token",
    "generate_pkce",
    "get_authorize_url",
    "get_flow",
    "get_pkce_challenge",
    "poll_device_code_flow",
    "qwen_get_pkce_challenge",
    "qwen_poll_for_token",
    "qwen_refresh_token",
    "qwen_request_device_code",
    "refresh_token",
    "register_flow",
    "start_oauth_callback_server",
    "wait_for_callback_or_manual_input",
]
