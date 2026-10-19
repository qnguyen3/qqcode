from __future__ import annotations

import pytest

from vibe.core.config import ProviderConfig
from vibe.core.llm.session_headers import session_headers


@pytest.mark.parametrize("name", ["opencode", "opencode-go"])
def test_opencode_providers_send_the_conversation_session(name: str) -> None:
    provider = ProviderConfig(name=name, api_base="https://opencode.ai/zen/v1")

    assert session_headers(provider, "conversation-1") == {
        "x-opencode-session": "conversation-1"
    }


def test_other_providers_send_no_session_header() -> None:
    provider = ProviderConfig(name="openai", api_base="https://api.openai.com/v1")

    assert session_headers(provider, "conversation-1") == {}
