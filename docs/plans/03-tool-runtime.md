# Plan 03 — Tool Runtime: parallel, timeout, structured results, truncation

> Gap: H2, H3, H4, H5, H11, H12, X11 · Mức độ: G2×6, G1×1 · Wave 1

## 1. Mục tiêu

1. Tool call độc lập chạy song song; approval vẫn tuần tự để đảm bảo an toàn UX.
2. Mọi tool có timeout mặc định (không chỉ `bash`).
3. Tool result có cấu trúc (`status`, `truncated`, nội dung model-facing tách biệt).
4. Truncation chuẩn hoá: line + byte, head/tail, notice rõ ràng, bash spill temp file.
5. Thêm `find`/`ls`; tuần tự hoá ghi cùng path để tránh race khi parallel.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Vòng lặp tool tuần tự | `vibe/core/agent.py:551-751` |
| Chỉ bash có timeout | `vibe/core/tools/builtins/bash.py:242-246`; `BashArgs.timeout` |
| Result bị string hoá `k: v` | `vibe/core/agent.py:641-651` |
| Truncation byte-cut mất đầu output bash | `bash.py:347-357` |
| Giới hạn rời rạc từng tool | `read_file.py:51-53` (64KB), `bash.py:133-135` (16KB), `grep.py:32-37` |
| Không có `find`/`ls` | `vibe/core/tools/builtins/` chỉ có 8 file tool |
| Không có lock theo path | `edit.py:84-123`; `write_file.py` |

## 3. Thiết kế

### 3.1 Truncation framework (`vibe/core/tools/truncate.py`)

```python
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_CHARS = 500
BASH_MAX_LINES = 2000
BASH_MAX_BYTES = 50 * 1024

@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: Literal["lines", "bytes", "none"]
    total_lines: int | None
    first_line: int | None      # 1-indexed trong output gốc
    last_line: int | None
    full_output_path: Path | None = None

def truncate_head(text: str, *, max_lines: int, max_bytes: int) -> TruncationResult
def truncate_tail(text: str, *, max_lines: int, max_bytes: int) -> TruncationResult
def spill_to_temp_file(text: str, prefix: str = "qqcode-tool") -> Path:
    """Ghi full output vào $TMPDIR; xoá khi process kết thúc."""
def format_notice(result: TruncationResult) -> str:
    """Sinh câu thông báo cho model, không bao giờ cắt giữa dòng:
    [Showing lines A-B of N. Full output: /tmp/...]"""
```

Quy tắc: không trả về dòng bị cắt dở (trừ khi byte-limit rơi vào 1 dòng dài → ghi rõ "line is X, showing first Y bytes").

### 3.2 Tool result có cấu trúc (`vibe/core/tools/base.py`)

Giữ tương thích result model hiện tại, thêm protocol:

```python
class ToolResultFormatter(Protocol):
    def format_for_model(self) -> str: ...      # text gửi model
    def ui_summary(self) -> str: ...            # dùng cho ToolResultDisplay

class BaseToolResult(BaseModel):
    """Base tuỳ chọn cho result model mới. Tool cũ không bắt buộc kế thừa."""
    truncated: bool = False
    full_output_path: str | None = None

    def format_for_model(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.model_dump(exclude_none=True).items())
```

`Agent` khi tạo tool response:

```python
text = (
    result_model.format_for_model()
    if hasattr(result_model, "format_for_model")
    else _default_format(result_model)
)
```

Migrate dần: plan này chuyển `bash`, `read_file`, `grep` sang `format_for_model` (nội dung có cấu trúc + notice); các tool còn lại giữ default.

### 3.3 Timeout

- `BaseToolConfig.timeout: float | None = None` (giây); `ToolManager.get_tool_config` merge như các field khác.
- Trong Agent:
  ```python
  async def _invoke_with_timeout(tool, args, timeout) -> BaseModel:
      if timeout is None:
          return await tool.invoke(**args)
      return await asyncio.wait_for(tool.invoke(**args), timeout=timeout)
  ```
- Timeout → `ToolError(f"Tool timed out after {timeout}s")` → tool result lỗi (model thấy lỗi, loop tiếp tục).
- Default gợi ý: `bash` giữ `default_timeout=30`, các tool khác `timeout=120` (config được). MCP tool mặc định 60s.

### 3.4 Parallel execution

Thiết kế an toàn với approval và side effect:

```
Giai đoạn A — assess + approve (tuần tự):
  for each call: resolve, permission, approval callback
  → danh sách (call, approved) và danh sách skip

Giai đoạn B — execute (song song giới hạn):
  semaphore = asyncio.Semaphore(config.tools.max_parallel_tools or 4)
  tasks = [execute_with_limit(call) for call in approved]
  results = await asyncio.gather(*tasks, return_exceptions=True)

Giai đoạn C — commit messages (tuần tự, đúng thứ tự model trả):
  append ToolResultEvent + tool message theo thứ tự calls gốc
```

Quy tắc:
- Tool có `execution_mode = "sequential"` (ClassVar mới trên `BaseTool`, default `"parallel"`) hoặc bị đánh dấu bởi config `tools.sequential = ["edit", "write_file"]` sẽ chạy ở lượt riêng tuần tự.
- Streaming event (`ToolCallEvent`/`ToolResultEvent`) phát theo thứ tự gốc để UI ổn định, dù execution hoàn tất lệch.
- Statistic counters cập nhật trong commit phase để tránh race.

### 3.5 File mutation queue (`vibe/core/tools/file_mutation_queue.py`)

```python
class FileMutationQueue:
    """Khoá theo path resolve; dùng chung cho write_file/edit/apply_patch tương lai."""
    async def run(self, path: Path, coro_factory: Callable[[], Awaitable[T]]) -> T: ...
```
- `ToolManager` giữ một instance và inject vào tool qua `ToolState` (hoặc module-level singleton có reset trong test).
- Đảm bảo hai edit song song cùng file thực thi tuần tự.

### 3.6 Tool `find` và `ls`

`find`:
```python
class FindArgs(BaseModel):
    pattern: str                    # glob, khớp full path
    path: str = "."
    limit: int | None = None        # default 1000
```
- Ưu tiên `fd` nếu có (`fd --full-path`), fallback walk bằng `pathlib.Path.rglob` bỏ qua `.gitignore` tối giản (tái dùng `ProjectContextProvider._is_ignored`).
- Truncation: 1000 kết quả / 50KB; notice.

`ls`:
```python
class LsArgs(BaseModel):
    path: str = "."
    limit: int | None = None        # default 500
```
- Sắp xếp alphabet, hậu tố `/` cho thư mục, hiện dotfiles; 50KB.

Cập nhật `bash.md` (hướng dẫn không dùng `ls`/`find` qua bash) và thêm `find.md`, `ls.md`.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 3.1 | Truncation framework + test | `vibe/core/tools/truncate.py` (mới) | M | — |
| 3.2 | `format_for_model` protocol + migrate bash/read/grep | `tools/base.py`, builtins | M | 3.1 |
| 3.3 | `BaseToolConfig.timeout` + invoke wrapper | `tools/base.py`, `agent.py` | M | — |
| 3.4 | Parallel execution (3 phase) + `execution_mode` | `agent.py`, `tools/base.py` | L | 3.3 |
| 3.5 | File mutation queue + tích hợp edit/write | `tools/file_mutation_queue.py` (mới) | M | 3.4 |
| 3.6 | Bash tail truncation + temp file | `bash.py` | M | 3.1 |
| 3.7 | Read head truncation + notice offset | `read_file.py` | S | 3.1 |
| 3.8 | Grep/find/ls chuẩn hoá limit | `grep.py` | S | 3.1 |
| 3.9 | Tool `find` + `ls` + prompt files | `tools/builtins/find.py`, `ls.py` (mới) | M | 3.1 |
| 3.10 | UI: hiển thị notice/temp path trong tool result | `textual_ui/renderers/tool_renderers.py` | S | 3.2 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/tools/test_truncate.py` | head/tail lines+bytes; không cắt dở dòng; notice format |
| `tests/tools/test_tool_timeout.py` | tool treo bị timeout, loop tiếp tục, stats đúng |
| `tests/test_agent_tool_call.py` | cập nhật: 2 tool call độc lập chạy song song (đo bằng thời gian/event order) |
| `tests/test_agent_parallel_approval.py` | approval vẫn tuần tự; skip không chạy |
| `tests/tools/test_file_queue.py` | 2 edit cùng file không đè nhau |
| `tests/tools/test_bash_output.py` | output 100k dòng → tail + temp file + notice |
| `tests/tools/test_find_ls.py` | pattern/path/limit; gitignore cơ bản |

## 6. DoD

- [ ] Hai tool call độc lập hoàn tất nhanh hơn tuần tự (test với sleep).
- [ ] Approval prompts không chồng lên nhau.
- [ ] Mọi builtin tool có timeout hiệu lực; `bash` giữ override.
- [ ] Bash/read/grep trả notice khi truncate và giữ phần hữu ích (tail với bash).
- [ ] `find`/`ls` có mặt, được test và có prompt.
- [ ] Mixed-version: config tool cũ không có `timeout` vẫn chạy.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Parallel làm hỏng thứ tự UI/event | commit phase tuần tự theo thứ tự gốc; test event order |
| Approval song song gây khó hiểu | giữ approval tuần tự bắt buộc |
| Tool tự ghi state (todo) chạy song song | đánh dấu `execution_mode="sequential"` cho todo/skill/submit_plan |
| Temp file tồn đọng | đăng ký `atexit` xoá; giới hạn kích thước; log path |

Rollback: config `tools.parallel=false` đưa về hành vi tuần tự; `tools.timeout` có thể disable.

## 8. Commit plan

```
feat(tools): add shared head and tail output truncation
feat(tools): send structured tool results to the model
feat(tools): apply a default timeout to every tool
feat(tools): run independent tool calls in parallel
fix(tools): serialize concurrent edits to the same file
feat(tools): keep bash tail output and spill full logs to a temp file
feat(tools): add find and ls tools for file discovery
```
