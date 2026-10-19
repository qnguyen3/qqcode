from __future__ import annotations

from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules

from vibe.core.llm.backend.adapters.anthropic_messages import AnthropicAdapter
from vibe.core.llm.backend.adapters.base import (
    ADAPTER_REGISTRY,
    BACKEND_ADAPTERS,
    APIAdapter,
    PreparedRequest,
    UnknownAPIStyleError,
    get_adapter,
    register_adapter,
)
from vibe.core.llm.backend.adapters.bedrock_converse import BedrockConverseAdapter
from vibe.core.llm.backend.adapters.google_generative_ai import (
    GoogleGenerativeAIAdapter,
)
from vibe.core.llm.backend.adapters.google_vertex import GoogleVertexAdapter
from vibe.core.llm.backend.adapters.openai_chat import OpenAIAdapter
from vibe.core.llm.backend.adapters.openai_codex_responses import (
    OpenAICodexResponsesAdapter,
)
from vibe.core.llm.backend.adapters.openai_responses import OpenAIResponsesAdapter
from vibe.core.llm.backend.adapters.pi_messages import PiMessagesAdapter

__all__ = [
    "ADAPTER_REGISTRY",
    "BACKEND_ADAPTERS",
    "APIAdapter",
    "AnthropicAdapter",
    "BedrockConverseAdapter",
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


def _load_adapter_modules() -> None:
    """Import every adapter module so its ``@register_adapter`` runs."""
    package_dir = Path(__file__).resolve().parent
    for module_info in iter_modules([str(package_dir)]):
        if module_info.name in {"base", "__init__"}:
            continue
        import_module(f"{__name__}.{module_info.name}")


_load_adapter_modules()
