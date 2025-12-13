from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Markdown, Static
from textual.widgets._markdown import MarkdownStream


class UserMessage(Static):
    def __init__(self, content: str, pending: bool = False) -> None:
        super().__init__()
        self.add_class("user-message")
        self._content = content
        self._pending = pending

    def compose(self) -> ComposeResult:
        with Horizontal(classes="user-message-container"):
            yield Static("> ", classes="user-message-prompt")
            yield Static(self._content, markup=False, classes="user-message-content")
            if self._pending:
                self.add_class("pending")

    async def set_pending(self, pending: bool) -> None:
        if pending == self._pending:
            return

        self._pending = pending

        if pending:
            self.add_class("pending")
            return

        self.remove_class("pending")


class AssistantMessage(Static):
    def __init__(self, content: str, reasoning_content: str | None = None) -> None:
        super().__init__()
        self.add_class("assistant-message")
        self._content = content
        self._reasoning_content = reasoning_content or ""
        self._markdown: Markdown | None = None
        self._stream: MarkdownStream | None = None
        self._reasoning_widget: Static | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="assistant-message-container"):
            yield Static("● ", classes="assistant-message-dot")
            with Vertical(classes="assistant-message-content"):
                # Reasoning content area (for thinking tokens)
                reasoning = Static("", classes="assistant-reasoning hidden")
                self._reasoning_widget = reasoning
                yield reasoning
                # Main content area
                markdown = Markdown("")
                self._markdown = markdown
                yield markdown

    def _get_markdown(self) -> Markdown:
        if self._markdown is None:
            self._markdown = self.query_one(Markdown)
        return self._markdown

    def _get_reasoning_widget(self) -> Static:
        if self._reasoning_widget is None:
            self._reasoning_widget = self.query_one(".assistant-reasoning", Static)
        return self._reasoning_widget

    def _ensure_stream(self) -> MarkdownStream:
        if self._stream is None:
            self._stream = Markdown.get_stream(self._get_markdown())
        return self._stream

    async def append_content(self, content: str) -> None:
        if not content:
            return

        self._content += content
        stream = self._ensure_stream()
        await stream.write(content)

    async def append_reasoning_content(self, reasoning: str) -> None:
        if not reasoning:
            return

        self._reasoning_content += reasoning
        widget = self._get_reasoning_widget()
        widget.update(self._reasoning_content)
        widget.remove_class("hidden")

    async def write_initial_content(self) -> None:
        if self._reasoning_content:
            widget = self._get_reasoning_widget()
            widget.update(self._reasoning_content)
            widget.remove_class("hidden")
        if self._content:
            stream = self._ensure_stream()
            await stream.write(self._content)

    async def stop_stream(self) -> None:
        if self._stream is None:
            return

        await self._stream.stop()
        self._stream = None


class UserCommandMessage(Static):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.add_class("user-command-message")
        self._content = content

    def compose(self) -> ComposeResult:
        yield Markdown(self._content)


class InterruptMessage(Static):
    def __init__(self) -> None:
        super().__init__(
            "Interrupted · What should Vibe do instead?", classes="interrupt-message"
        )


class BashOutputMessage(Static):
    def __init__(self, command: str, cwd: str, output: str, exit_code: int) -> None:
        super().__init__()
        self.add_class("bash-output-message")
        self._command = command
        self._cwd = cwd
        self._output = output
        self._exit_code = exit_code

    def compose(self) -> ComposeResult:
        with Vertical(classes="bash-output-container"):
            with Horizontal(classes="bash-cwd-line"):
                yield Static(self._cwd, markup=False, classes="bash-cwd")
                yield Static("", classes="bash-cwd-spacer")
                if self._exit_code == 0:
                    yield Static("✓", classes="bash-exit-success")
                else:
                    yield Static("✗", classes="bash-exit-failure")
                    yield Static(f" ({self._exit_code})", classes="bash-exit-code")
            with Horizontal(classes="bash-command-line"):
                yield Static("> ", classes="bash-chevron")
                yield Static(self._command, markup=False, classes="bash-command")
                yield Static("", classes="bash-command-spacer")
            yield Static(self._output, markup=False, classes="bash-output")


class ErrorMessage(Static):
    def __init__(self, error: str, collapsed: bool = True) -> None:
        super().__init__(classes="error-message")
        self._error = error
        self.collapsed = collapsed

    def compose(self) -> ComposeResult:
        if self.collapsed:
            yield Static("Error. (ctrl+o to expand)", markup=False)
        else:
            yield Static(f"Error: {self._error}", markup=False)

    def set_collapsed(self, collapsed: bool) -> None:
        if self.collapsed == collapsed:
            return

        self.collapsed = collapsed
        self.remove_children()

        if self.collapsed:
            self.mount(Static("Error. (ctrl+o to expand)", markup=False))
        else:
            self.mount(Static(f"Error: {self._error}", markup=False))
