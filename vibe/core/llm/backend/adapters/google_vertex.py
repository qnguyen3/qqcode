from __future__ import annotations

from typing import ClassVar

from vibe.core.llm.backend.adapters.base import register_adapter
from vibe.core.llm.backend.adapters.google_generative_ai import (
    GoogleGenerativeAIAdapter,
)


@register_adapter("google-vertex")
class GoogleVertexAdapter(GoogleGenerativeAIAdapter):
    """Vertex AI variant of the native ``generateContent`` adapter.

    The request/response payloads are identical to Gemini; only the URL layout
    differs. Project and location are folded into the provider base URL by the
    ambient resolver, so the adapter appends the Vertex publisher path here.
    """

    endpoint: ClassVar[str] = "/publishers/google/models"
