from __future__ import annotations

from pathlib import Path

from acp import ReadTextFileRequest, ReadTextFileResponse, WriteTextFileRequest
import pytest

from vibe.acp.tools.builtins.edit import AcpEditState, Edit
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.edit import (
    EditArgs,
    EditConfig,
    EditResult,
)
from vibe.core.types import ToolCallEvent, ToolResultEvent


class MockConnection:
    def __init__(
        self,
        file_content: str = "original line 1\noriginal line 2\noriginal line 3",
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self._file_content = file_content
        self._read_error = read_error
        self._write_error = write_error
        self._read_text_file_called = False
        self._write_text_file_called = False
        self._session_update_called = False
        self._last_read_request: ReadTextFileRequest | None = None
        self._last_write_request: WriteTextFileRequest | None = None
        self._write_calls: list[WriteTextFileRequest] = []

    async def readTextFile(self, request: ReadTextFileRequest) -> ReadTextFileResponse:
        self._read_text_file_called = True
        self._last_read_request = request

        if self._read_error:
            raise self._read_error

        return ReadTextFileResponse(content=self._file_content)

    async def writeTextFile(self, request: WriteTextFileRequest) -> None:
        self._write_text_file_called = True
        self._last_write_request = request
        self._write_calls.append(request)

        if self._write_error:
            raise self._write_error

    async def sessionUpdate(self, notification) -> None:
        self._session_update_called = True


@pytest.fixture
def mock_connection() -> MockConnection:
    return MockConnection()


@pytest.fixture
def acp_edit_tool(mock_connection: MockConnection, tmp_path: Path) -> Edit:
    config = EditConfig(workdir=tmp_path)
    state = AcpEditState.model_construct(
        connection=mock_connection,  # type: ignore[arg-type]
        session_id="test_session_123",
        tool_call_id="test_tool_call_456",
    )
    return Edit(config=config, state=state)


class TestAcpEditBasic:
    def test_get_name(self) -> None:
        assert Edit.get_name() == "edit"


class TestAcpEditExecution:
    @pytest.mark.asyncio
    async def test_run_success(
        self,
        acp_edit_tool: Edit,
        mock_connection: MockConnection,
        tmp_path: Path,
    ) -> None:
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("original line 1\noriginal line 2\noriginal line 3")
        args = EditArgs(
            file_path=str(test_file),
            old_string="original line 2",
            new_string="modified line 2",
        )
        result = await acp_edit_tool.run(args)

        assert isinstance(result, EditResult)
        assert result.file == str(test_file)
        assert result.replacements == 1
        assert result.old_string == "original line 2"
        assert result.new_string == "modified line 2"
        assert mock_connection._read_text_file_called
        assert mock_connection._write_text_file_called
        assert mock_connection._session_update_called

        # Verify ReadTextFileRequest was created correctly
        read_request = mock_connection._last_read_request
        assert read_request is not None
        assert read_request.sessionId == "test_session_123"
        assert read_request.path == str(test_file)

        # Verify WriteTextFileRequest was created correctly
        write_request = mock_connection._last_write_request
        assert write_request is not None
        assert write_request.sessionId == "test_session_123"
        assert write_request.path == str(test_file)
        assert (
            write_request.content == "original line 1\nmodified line 2\noriginal line 3"
        )

    @pytest.mark.asyncio
    async def test_run_replace_all(
        self, mock_connection: MockConnection, tmp_path: Path
    ) -> None:
        mock_connection._file_content = "foo bar foo baz foo"
        test_file = tmp_path / "test.txt"
        test_file.write_text("foo bar foo baz foo")

        tool = Edit(
            config=EditConfig(workdir=tmp_path),
            state=AcpEditState.model_construct(
                connection=mock_connection,  # type: ignore[arg-type]
                session_id="test_session",
                tool_call_id="test_call",
            ),
        )

        args = EditArgs(
            file_path=str(test_file),
            old_string="foo",
            new_string="qux",
            replace_all=True,
        )
        result = await tool.run(args)

        assert result.replacements == 3
        write_request = mock_connection._last_write_request
        assert write_request is not None
        assert write_request.content == "qux bar qux baz qux"

    @pytest.mark.asyncio
    async def test_run_error_multiple_occurrences(
        self, mock_connection: MockConnection, tmp_path: Path
    ) -> None:
        mock_connection._file_content = "foo bar foo baz"
        test_file = tmp_path / "test.txt"
        test_file.write_text("foo bar foo baz")

        tool = Edit(
            config=EditConfig(workdir=tmp_path),
            state=AcpEditState.model_construct(
                connection=mock_connection,  # type: ignore[arg-type]
                session_id="test_session",
                tool_call_id="test_call",
            ),
        )

        args = EditArgs(
            file_path=str(test_file),
            old_string="foo",
            new_string="qux",
            replace_all=False,
        )
        with pytest.raises(ToolError) as exc_info:
            await tool.run(args)

        assert "appears 2 times" in str(exc_info.value)
        assert "replace_all=true" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_run_error_not_found(
        self, mock_connection: MockConnection, tmp_path: Path
    ) -> None:
        mock_connection._file_content = "hello world"
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        tool = Edit(
            config=EditConfig(workdir=tmp_path),
            state=AcpEditState.model_construct(
                connection=mock_connection,  # type: ignore[arg-type]
                session_id="test_session",
                tool_call_id="test_call",
            ),
        )

        args = EditArgs(
            file_path=str(test_file),
            old_string="not found",
            new_string="replacement",
        )
        with pytest.raises(ToolError) as exc_info:
            await tool.run(args)

        assert "old_string not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_run_read_error(
        self, mock_connection: MockConnection, tmp_path: Path
    ) -> None:
        mock_connection._read_error = RuntimeError("File not found")

        tool = Edit(
            config=EditConfig(workdir=tmp_path),
            state=AcpEditState.model_construct(
                connection=mock_connection,  # type: ignore[arg-type]
                session_id="test_session",
                tool_call_id="test_call",
            ),
        )

        test_file = tmp_path / "test.txt"
        test_file.touch()
        args = EditArgs(
            file_path=str(test_file),
            old_string="old",
            new_string="new",
        )
        with pytest.raises(ToolError) as exc_info:
            await tool.run(args)

        assert (
            str(exc_info.value)
            == f"Unexpected error reading {test_file}: File not found"
        )

    @pytest.mark.asyncio
    async def test_run_write_error(
        self, mock_connection: MockConnection, tmp_path: Path
    ) -> None:
        mock_connection._write_error = RuntimeError("Permission denied")
        test_file = tmp_path / "test.txt"
        test_file.touch()
        mock_connection._file_content = "old"

        tool = Edit(
            config=EditConfig(workdir=tmp_path),
            state=AcpEditState.model_construct(
                connection=mock_connection,  # type: ignore[arg-type]
                session_id="test_session",
                tool_call_id="test_call",
            ),
        )

        args = EditArgs(
            file_path=str(test_file),
            old_string="old",
            new_string="new",
        )
        with pytest.raises(ToolError) as exc_info:
            await tool.run(args)

        assert str(exc_info.value) == f"Error writing {test_file}: Permission denied"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "connection,session_id,expected_error",
        [
            (
                None,
                "test_session",
                "Connection not available in tool state. This tool can only be used within an ACP session.",
            ),
            (
                MockConnection(),
                None,
                "Session ID not available in tool state. This tool can only be used within an ACP session.",
            ),
        ],
    )
    async def test_run_without_required_state(
        self,
        tmp_path: Path,
        connection: MockConnection | None,
        session_id: str | None,
        expected_error: str,
    ) -> None:
        test_file = tmp_path / "test.txt"
        test_file.touch()
        tool = Edit(
            config=EditConfig(workdir=tmp_path),
            state=AcpEditState.model_construct(
                connection=connection,  # type: ignore[arg-type]
                session_id=session_id,
                tool_call_id="test_call",
            ),
        )

        args = EditArgs(
            file_path=str(test_file),
            old_string="old",
            new_string="new",
        )
        with pytest.raises(ToolError) as exc_info:
            await tool.run(args)

        assert str(exc_info.value) == expected_error


class TestAcpEditSessionUpdates:
    def test_tool_call_session_update(self) -> None:
        event = ToolCallEvent(
            tool_name="edit",
            tool_call_id="test_call_123",
            args=EditArgs(
                file_path="/tmp/test.txt",
                old_string="old text",
                new_string="new text",
            ),
            tool_class=Edit,
        )

        update = Edit.tool_call_session_update(event)
        assert update is not None
        assert update.sessionUpdate == "tool_call"
        assert update.toolCallId == "test_call_123"
        assert update.kind == "edit"
        assert update.title is not None
        assert update.content is not None
        assert len(update.content) == 1
        assert update.content[0].type == "diff"
        assert update.content[0].path == "/tmp/test.txt"
        assert update.content[0].oldText == "old text"
        assert update.content[0].newText == "new text"
        assert update.locations is not None
        assert len(update.locations) == 1
        assert update.locations[0].path == "/tmp/test.txt"

    def test_tool_call_session_update_invalid_args(self) -> None:
        class InvalidArgs:
            pass

        event = ToolCallEvent.model_construct(
            tool_name="edit",
            tool_call_id="test_call_123",
            args=InvalidArgs(),  # type: ignore[arg-type]
            tool_class=Edit,
        )

        update = Edit.tool_call_session_update(event)
        assert update is None

    def test_tool_result_session_update(self) -> None:
        result = EditResult(
            file="/tmp/test.txt",
            old_string="old text",
            new_string="new text",
            replacements=1,
        )

        event = ToolResultEvent(
            tool_name="edit",
            tool_call_id="test_call_123",
            result=result,
            tool_class=Edit,
        )

        update = Edit.tool_result_session_update(event)
        assert update is not None
        assert update.sessionUpdate == "tool_call_update"
        assert update.toolCallId == "test_call_123"
        assert update.status == "completed"
        assert update.content is not None
        assert len(update.content) == 1
        assert update.content[0].type == "diff"
        assert update.content[0].path == "/tmp/test.txt"
        assert update.content[0].oldText == "old text"
        assert update.content[0].newText == "new text"
        assert update.locations is not None
        assert len(update.locations) == 1
        assert update.locations[0].path == "/tmp/test.txt"

    def test_tool_result_session_update_error(self) -> None:
        event = ToolResultEvent(
            tool_name="edit",
            tool_call_id="test_call_123",
            result=None,
            error="Some error occurred",
            tool_class=Edit,
        )

        update = Edit.tool_result_session_update(event)
        assert update is not None
        assert update.sessionUpdate == "tool_call_update"
        assert update.toolCallId == "test_call_123"
        assert update.status == "failed"

    def test_tool_result_session_update_invalid_result(self) -> None:
        class InvalidResult:
            pass

        event = ToolResultEvent.model_construct(
            tool_name="edit",
            tool_call_id="test_call_123",
            result=InvalidResult(),  # type: ignore[arg-type]
            tool_class=Edit,
        )

        update = Edit.tool_result_session_update(event)
        assert update is None
