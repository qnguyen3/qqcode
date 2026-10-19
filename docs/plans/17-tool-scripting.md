# Plan 17 — Sandboxed tool scripting (codemode)

> Gap: H14 · Mức độ: G2×1 · Wave 3

## 1. Mục tiêu

1. Một tool `run_tools_script` cho phép model viết **một script ngắn** gọi nhiều tool có sẵn, xử lý/ lọc kết quả, trả về một output gọn — giảm số round-trip và tránh đưa dữ liệu thô khổng lồ vào context.
2. Script chạy trong **subprocess cô lập** với bridge JSON-RPC về `ToolManager`; không truy cập filesystem/network trực tiếp, chỉ qua tool được phép.
3. Mọi tool call trong script tuân theo **cùng permission/approval** như gọi trực tiếp; tool chưa được cấp quyền → từ chối trong script kèm lý do.
4. Giới hạn tài nguyên: timeout, số tool call, kích thước output, bộ nhớ (rlimit).
5. Off-by-default, `experimental`: bật qua config; tắt thì không ảnh hưởng gì.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Mỗi tool call là một round-trip LLM | vòng lặp tool `vibe/core/agent.py` |
| Không có cách compose nhiều tool trong 1 bước | không có |
| Kết quả tool lớn bị đưa vào context (đã truncate ở plan 03 nhưng vẫn tốn) | `vibe/core/tools/truncate.py` (plan 03) |
| Không có sandbox/script tool | không có |
| Permission hierarchy đã có | `vibe/core/tools/base.py:39-69`; `agent.py:_should_execute_tool` |
| `execution_mode` đã có trên tool (plan 03) | `vibe/core/tools/base.py:180` |

## 3. Thiết kế

### 3.1 Tool `run_tools_script` (`vibe/core/tools/builtins/run_tools_script.py`)

```python
class RunToolsScriptArgs(BaseModel):
    language: Literal["python"] = "python"
    code: str                       # script ngắn do model viết
    tool_allowlist: list[str] = []  # model khai báo tool sẽ gọi (để kiểm quyền trước)

class RunToolsScriptResult(BaseToolResult):
    output: str                     # stdout của script (đã cắt theo giới hạn)
    tool_calls: list[ScriptToolCall]  # name, ok, duration
    truncated: bool = False
    error: str | None = None
```

- Script chỉ tương tác với thế giới qua đối tượng `tools`:

```python
# ví dụ script model viết
paths = [m["path"] for m in tools.grep(pattern="TODO", path="vibe").matches]
todo = [p for p in paths if p.endswith(".py")]
print(f"{len(todo)} files with TODO")
for p in todo[:5]:
    print(p)
```

- `tools.<name>(**args)` trả dict kết quả (đã `format_for_model`/model_dump). `print()` → `output`.

### 3.2 Sandbox runner (`vibe/core/tools/script_sandbox.py`)

```python
class ScriptSandbox:
    async def run(
        self,
        code: str,
        *,
        allowed_tools: set[str],
        bridge: ToolBridge,
        timeout: float,
        max_calls: int,
        max_output_bytes: int,
    ) -> RunToolsScriptResult: ...
```

- Thực thi bằng `asyncio.create_subprocess_exec(sys.executable, "-I", worker_path)` chạy một **worker cố định** (`vibe/core/tools/_script_worker.py`) — không eval code trong tiến trình agent.
- Worker: `exec(code, restricted_globals)` với:
  - builtins allowlist (`print`, `len`, `range`, `sorted`, `str`, `int`, `dict`, `list`, `set`, `min`, `max`, `sum`, `enumerate`, `zip`, ...); **không** `open`/`__import__`/`eval`/`exec`/`compile`.
  - `__import__` bị chặn; chỉ module an toàn được preload (`json`, `re`, `math`, `statistics`).
- Bridge: worker gửi JSON-RPC `{"call": name, "args": {...}}` qua stdout tới tiến trình cha; cha kiểm `allowed_tools` (giao của model khai báo, permission hiện hành, `scriptable=True`) rồi mới gọi tool thật; trả kết quả lại cho worker.
- Giới hạn: `timeout` (mặc định 30s), `max_calls` (mặc định 20), `max_output_bytes` (mặc định 16KB), `RLIMIT_AS` (mặc định 256MB) set trong worker.
- Không có mạng: worker chạy với env tối thiểu; (ghi rõ: đây là cô lập mức tiến trình, không phải micro-VM; docs cảnh báo).

### 3.3 Permission & an toàn

- Trước khi chạy, cha kiểm `tool_allowlist ⊆ {tool active và scriptable}`; tool cần approval (`ASK`) **không** được gọi trong script trừ khi mode auto-approve hoặc đã "approve always".
- Mỗi tool call trong script vẫn ghi audit log / phát `ToolCallEvent` (plan 08/10) như gọi trực tiếp.
- Tool đánh dấu `scriptable: ClassVar[bool] = True` mặc định cho read-only (`read_file`, `grep`, `find`, `ls`); `write_file`/`edit`/`bash` mặc định `scriptable=False` (bật có chủ đích + cảnh báo).

### 3.4 Config & prompt

```toml
[tools.script]
enabled = false            # experimental, off mặc định
timeout = 30
max_calls = 20
max_output_bytes = 16384
memory_mb = 256
```

- Prompt `run_tools_script.md`: mô tả API `tools.*`, giới hạn, và khi nào nên dùng (lọc/tổng hợp nhiều kết quả tool).

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 17.1 | `scriptable` ClassVar + config `[tools.script]` | `tools/base.py`, `config.py` | S | — |
| 17.2 | Worker cô lập + restricted builtins + rlimit | `tools/_script_worker.py` (mới) | L | — |
| 17.3 | `ScriptSandbox` + bridge JSON-RPC + timeout/call/output cap | `tools/script_sandbox.py` (mới) | L | 17.2 |
| 17.4 | Tool `run_tools_script` + result model + prompt | `tools/builtins/run_tools_script.py`, prompts | M | 17.3 |
| 17.5 | Permission gate + audit/event cho tool call trong script | `tools/script_sandbox.py`, `agent.py` | M | plan 08/10 |
| 17.6 | Tests sandbox (allowlist, vượt giới hạn, không import/open) | `tests/tools/test_script_*.py` | M | 17.3 |
| 17.7 | Docs `docs/tool-scripting.md` + cảnh báo bảo mật | docs | S | 17.4 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/tools/test_script_run.py` | script gọi `grep`/`read_file`, in output; `tool_calls` ghi đúng |
| `tests/tools/test_script_sandbox.py` | chặn `import os`, `open`, `eval`; không có mạng |
| `tests/tools/test_script_limits.py` | vượt timeout/max_calls/max_output → dừng an toàn, kết quả báo lý do |
| `tests/tools/test_script_permission.py` | tool ngoài allowlist bị từ chối; tool cần approval không tự chạy |
| `tests/test_config_migration.py` | `[tools.script]` thiếu vẫn chạy; `enabled=false` không đăng ký tool |

## 6. DoD

- [x] `enabled=false` (mặc định): tool không xuất hiện, không ảnh hưởng gì.
- [x] Bật: script gọi được tool read-only, trả output gọn; sandbox chặn import/open/eval.
- [x] Vượt giới hạn dừng an toàn và báo rõ; subprocess luôn bị kill khi hết timeout.
- [x] Tool call trong script tuân thủ permission + ghi audit.
- [x] `ruff`, `pyright`, `pytest` xanh.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Thoát sandbox (RCE) | subprocess riêng + builtins allowlist + no import + no net + rlimit; mặc định tắt; docs nói rõ không phải micro-VM |
| Script treo/kill process cha | timeout + `kill` cả process group; worker không kế thừa fd nguy hiểm |
| Ghi dữ liệu ngoài ý muốn | tool mutating `scriptable=False` mặc định; allowlist tường minh |
| Bỏ sót event/audit | bridge phát cùng event như gọi trực tiếp; test |

Rollback: `tools.script.enabled=false` (mặc định) → tool biến mất, code sandbox không được import khi tắt.

## 8. Commit plan

```
feat(tools): run tool scripts in an isolated worker
feat(tools): bridge script tool calls through the tool manager
feat(tools): enforce permissions and limits for scripts
docs(tools): document sandboxed tool scripting
```
