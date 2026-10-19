# Plan 11 — Subagent Tool (task delegation)

> Gap: S7, H9 · Mức độ: G3×1, G2×1 · Wave 4

## 1. Mục tiêu

1. Tool `task` cho phép agent chính giao một việc con cho agent phụ có context riêng.
2. Agent phụ có toolset hạn chế, budget riêng (turn/price/time), session riêng, không thể spawn tiếp.
3. Kết quả trả về agent chính là text cuối + tóm tắt usage; UI hiển thị tiến trình tối thiểu.
4. Không cần durability/task graph ở phase này (đó là hướng mở rộng sau).

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Không có delegation | `vibe/core/tools/builtins/` không có task tool |
| Agent tạo được độc lập | `Agent.__init__` nhận config/backend/mode (`agent.py:107-190`) |
| Có sẵn `max_turns`/`max_price` cho programmatic | `agent.py:242-257`; `programmatic.py:144-181` |
| UI streaming event sẵn sàng mở rộng | `ToolCallEvent`/`ToolResultEvent` (`types.py:246-261`) |

## 3. Thiết kế

### 3.1 Tool `task` (`vibe/core/tools/builtins/task.py`)

```python
class TaskArgs(BaseModel):
    description: str = Field(description="3-5 từ mô tả ngắn, dùng cho UI")
    prompt: str = Field(description="Nhiệm vụ đầy đủ cho agent con")
    agent: str | None = Field(default=None, description="Tên agent config trong ~/.qqcode/agents (mặc định: general)")
    max_turns: int | None = Field(default=15, description="Giới hạn số turn của agent con")

class TaskResult(BaseModel):
    description: str
    output: str
    turns: int
    prompt_tokens: int
    completion_tokens: int
    success: bool
    error: str | None = None

    def format_for_model(self) -> str: ...
```

Prompt tool (`prompts/task.md`) hướng dẫn:
- Dùng khi công việc độc lập, tốn nhiều tool call, không cần user tương tác.
- Không giao việc cần approval tương tác (agent con chạy auto-approve có kiểm soát).
- Cung cấp đủ context; agent con không thấy history của cha.

### 3.2 Subagent runner (`vibe/core/subagent.py`)

```python
class SubAgentRunner:
    def __init__(self, parent: Agent) -> None: ...
    async def run(self, args: TaskArgs) -> TaskResult:
        """1. Nếu agent config: load VibeConfig(agent=args.agent) + merge overrides
              (toolset read-only mặc định, auto-approve, max_turns).
           2. Tạo Agent con với session độc lập (plan 02), không hook spawn,
              backend dùng lại parent backend factory (không tạo client thừa).
           3. Chạy act(prompt), thu AssistantEvent cuối; đếm turn/usage.
           4. Trả TaskResult; luôn stop session con (flush JSONL)."""
```

Hạn chế cứng của agent con:
- Toolset: mặc định `[read_file, grep, find, ls, bash(allowlist chỉ đọc?)]` — cấu hình qua agent TOML (`disabled_tools=["task", "skill"]` mặc định).
- Không có `task` tool (chặn đệ quy) bằng cách loại khỏi `_available`.
- Mode `AUTO_APPROVE` trong sandbox workdir của cha; **không** auto-approve nếu task yêu cầu ghi ngoài workdir (kiểm tra path trong `before_tool_call` hook dùng plan 08 nếu có; nếu chưa có, dùng `path_allowlist` plan 10).
- Budget: `max_turns` (default 15), `max_price` mặc định = null; timeout cứng 10 phút (config `subagent.timeout`).

### 3.3 Đồng thời & giới hạn

- `subagent.max_concurrent: int = 2`; vượt → tool trả lỗi "too many concurrent subagents" (model biết để chờ).
- Mỗi task con chạy trong `asyncio.Task`; cancellation từ cha (abort) huỷ task con.
- Agent con chạy **không streaming ra UI chính** mặc định; phát event tóm tắt `SubagentEvent(description, status)` để UI hiển thị spinner/nhãn. Config `subagent.stream_tools: bool = False` (nếu bật, forward ToolCallEvent với prefix `[sub]`).

### 3.4 Session con

- Session riêng trong cùng thư mục project slug, `parent_session_id` trỏ session cha (plan 02).
- Không ghi vào messages của cha; chỉ append một `ToolResult` message.
- `TaskResult` lưu entry `custom(key="subagent_result")` để `/session` thống kê.

### 3.5 UI

- `ToolCallEvent(task)` render tiêu đề `task: <description>`; khi xong hiển thị `turns/tokens`.
- Nếu >2s chưa xong: dòng trạng thái "subagent running (N turns)..." cập nhật mỗi 500ms từ event tóm tắt.
- Lỗi/timeout hiển thị `ToolResultDisplay(success=False)`.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 11.1 | `task` tool args/result/prompt | `tools/builtins/task.py`, `prompts/task.md` (mới) | M | plan 02/03 |
| 11.2 | `SubAgentRunner` (config merge, chạy, thu kết quả) | `vibe/core/subagent.py` (mới) | L | 11.1 |
| 11.3 | Chặn đệ quy + giới hạn toolset | `subagent.py` | S | 11.2 |
| 11.4 | Budget + timeout + concurrent limit | `subagent.py`, `config.py` | M | 11.2 |
| 11.5 | UI summary event + render | `types.py`, `textual_ui/` | M | 11.2 |
| 11.6 | Ghi subagent session + custom entry | `subagent.py`, sessions | S | plan 02 |
| 11.7 | Cancellation khi cha abort | `agent.py`, `subagent.py` | M | 11.2 |
| 11.8 | Docs `docs/subagents.md` + agent examples | docs, `examples/agents/` | M | 11.4 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/subagent/test_runner.py` | FakeBackend: task con chạy, trả output, turn/usage đúng |
| `tests/subagent/test_recursion_blocked.py` | tool `task` không tồn tại trong toolset con |
| `tests/subagent/test_budget.py` | vượt max_turns → success=false, error rõ |
| `tests/subagent/test_concurrency.py` | > max_concurrent trả lỗi; cancel khi cha abort |
| `tests/subagent/test_session.py` | session con ghi JSONL, parent_session_id đúng |
| `tests/test_agent_tool_call.py` | task tool xuất hiện trong tool list cha (khi bật) |

## 6. DoD

- [ ] Agent chính gọi `task`, nhận output text cuối của agent con trong test end-to-end (FakeBackend).
- [ ] Agent con không spawn được agent con; không sửa history cha ngoài tool result.
- [ ] Budget/timeout/concurrent được enforce và có test.
- [ ] Abort cha huỷ task con, session con flush.
- [ ] Config `subagent.enabled=false` (default **true**? — chọn default `true` nhưng có thể tắt) hoạt động.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Agent con dùng quyền ghi nguy hiểm | toolset read-only + path allowlist + auto-approve có kiểm soát; docs cảnh báo |
| Tốn token ngoài kiểm soát | max_turns mặc định 15, timeout 10 phút, concurrent 2; usage ghi lại |
| Recursion gián tiếp qua MCP tool | MCP tool của con bị giới hạn bởi config con; không expose task |
| Deadlock khi 2 subagent chờ nhau | không cho con gọi task; timeout cứng |

Rollback: `subagent.enabled=false` ẩn tool; không ảnh hưởng luồng hiện tại.

## 8. Commit plan

```
feat(tools): add a task tool for delegated work
feat(subagent): run child agents with isolated context and budgets
feat(subagent): limit child tools and prevent nested delegation
feat(ui): show delegated task progress
fix(subagent): cancel child work when the parent is aborted
docs(subagent): document configuration and limits
```
