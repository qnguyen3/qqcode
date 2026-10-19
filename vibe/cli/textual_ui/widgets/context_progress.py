from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from textual.reactive import reactive
from textual.widgets import Static


@dataclass
class TokenState:
    max_tokens: int = 0
    current_tokens: int = 0


class ContextProgress(Static):
    tokens = reactive(TokenState())
    model = reactive("")
    cost_usd = reactive(0.0)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def watch_tokens(self, new_state: TokenState) -> None:
        self._refresh_text()

    def watch_model(self, new_model: str) -> None:
        self._refresh_text()

    def watch_cost_usd(self, new_cost: float) -> None:
        self._refresh_text()

    def _refresh_text(self) -> None:
        state = self.tokens
        if state.max_tokens == 0:
            self.update("")
            return

        percentage = min(100, int((state.current_tokens / state.max_tokens) * 100))
        text = f"{percentage}% of {state.max_tokens // 1000}k tokens"
        if self.model:
            text = f"{self.model} · {text}"
        if self.cost_usd:
            text = f"{text} · ${self.cost_usd:.4f}"
        self.update(text)
