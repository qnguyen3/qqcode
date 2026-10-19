# Plan 08 — Extensibility: Hook API, Skills parity, Resource Packages

> Gap: E1, E4, E11, E13, E14, E15, E16, X13 · Mức độ: G3×2, G2×6 · Wave 3

## 1. Mục tiêu

1. Hook/event API tối thiểu để mở rộng hành vi mà không sửa core: `before_request`, `after_response`, `tool_call`, `tool_result`, `agent_start/end`, `build_context`, `before_compact/after_compact/compact_failed`.
2. Custom extension file-based: `.qqcode/hooks/*.py` (project) và `~/.qqcode/hooks/*.py` (global).
3. Skill parity hoàn chỉnh (đã thiết kế ở plan 04 — plan này kiểm tra tích hợp + extension gọi skill).
4. Resource package: manifest `qqcode.toml` + `qqcode install/list/remove` (git/local), phân phối tools/skills/prompts/agents/hooks.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Không có hook/event | chỉ có `message_observer` cho UI (`agent.py:113`, `223-235`) |
| Custom tool drop-in file .py | `tools/manager.py:58-120` |
| Skills chỉ name/description | `skills/manager.py:89-116` |
| Không package manager | không có code |
| Custom agent/prompt file rời | `config.py:753-764`, `config.py:494-503` |

## 3. Thiết kế

### 3.1 Event model (`vibe/core/hooks/events.py`)

```python
class HookEvent(BaseModel):
    type: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

class AgentStartEvent(HookEvent):
    type: Literal["agent_start"] = "agent_start"
    session_id: str
    prompt: str

class BeforeRequestEvent(HookEvent):
    type: Literal["before_request"] = "before_request"
    messages: list[LLMMessage]
    model_alias: str

class AfterResponseEvent(HookEvent):
    type: Literal["after_response"] = "after_response"
    message: LLMMessage
    usage: LLMUsage

class ToolCallEventHook(HookEvent):
    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_call_id: str
    args: dict[str, Any]

class ToolResultEventHook(HookEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    tool_call_id: str
    ok: bool
    text: str | None
    error: str | None

class AgentEndEvent(HookEvent):
    type: Literal["agent_end"] = "agent_end"
    session_id: str
    usage: LLMUsage

class BeforeCompactEvent(HookEvent):
    type: Literal["before_compact"] = "before_compact"
    messages: list[LLMMessage]
    tokens_before: int
    reason: Literal["auto", "manual", "overflow"] = "auto"

class AfterCompactEvent(HookEvent):
    type: Literal["after_compact"] = "after_compact"
    tokens_before: int
    tokens_after: int
    is_update: bool

class CompactFailedEvent(HookEvent):
    type: Literal["compact_failed"] = "compact_failed"
    error: str
```

Quyết định thiết kế: hook **không chặn** (không veto) ở phase này. Muốn chặn tool → dùng `ToolPermission`/denylist hiện có; muốn gate theo path → extension `qqcode-protected-paths` (ví dụ kèm) dùng `tool_call` + raise? Không — để an toàn, cung cấp cơ chế `Decision`:

```python
class HookDecision(BaseModel):
    action: Literal["allow", "deny", "modify"] = "allow"
    reason: str | None = None
    replacement_args: dict[str, Any] | None = None
    injected_messages: list[LLMMessage] = []
    summary_override: str | None = None   # before_compact: cung cấp sẵn summary, bỏ qua LLM
```
`before_request`, `tool_call` và `before_compact` trả `HookDecision`; các event khác fire-and-forget.
- `before_compact`: `deny` → bỏ qua compact lượt này; `modify` + `summary_override` → dùng summary do hook cấp, không gọi LLM tóm tắt.

### 3.2 Registry & dispatch (`vibe/core/hooks/registry.py`)

```python
class HookRegistry:
    def register(self, event_type: str, handler: HookHandler, *, priority: int = 0) -> None: ...
    async def dispatch(self, event: HookEvent) -> HookDecision:
        """Chạy handler theo priority; handler lỗi -> log, không làm chết agent.
        Trả decision hợp nhất: deny thắng allow; modify gộp args (handler sau thắng)."""
```

- Handler có thể sync hoặc async; timeout mỗi handler 5s (config `hooks.timeout`) để extension hỏng không treo agent.
- Loading:
  ```
  ~/.qqcode/hooks/*.py      (global, luôn load)
  .qqcode/hooks/*.py        (project, chỉ load khi trust — plan 10)
  ```
  Mỗi file export `def register(registry: HookRegistry) -> None`.
- Import bằng `importlib` như `ToolManager` hiện tại; lỗi import bị log + skip (không chặn khởi động).

### 3.3 Điểm gọi trong Agent

| Thời điểm | Event | Xử lý decision |
|---|---|---|
| Bắt đầu `act()` | `agent_start` | inject messages nếu có |
| Trước `_chat*` | `before_request` | deny → dừng turn với thông báo; modify → thay messages |
| Sau `_chat*` | `after_response` | chỉ quan sát |
| Trước `invoke` | `tool_call` | deny → tool result bị skip kèm lý do; modify → args thay thế |
| Sau `invoke` | `tool_result` | chỉ quan sát |
| Trước compaction | `before_compact` | deny → bỏ qua compact; modify → `summary_override` thay LLM |
| Sau compaction | `after_compact` | chỉ quan sát |
| Compaction lỗi | `compact_failed` | chỉ quan sát |
| Kết thúc turn loop | `agent_end` | chỉ quan sát |

Tương thích: khi chưa có hook, chi phí = một dispatch rỗng (list rỗng → return allow), không ảnh hưởng hiệu năng đáng kể.

### 3.4 API extension cấp cao (tùy chọn đóng gói)

Để viết hook gọn, cung cấp helper module `vibe.core.hooks.api`:
```python
from vibe.core.hooks.api import on_tool_call, deny, modify, inject

@on_tool_call("bash")
def guard(event):
    if "rm -rf" in event.args.get("command", ""):
        return deny("destructive command blocked")
```

### 3.5 Resource package

Manifest `qqcode.toml` ở root repo/package:

```toml
[package]
name = "my-qqcode-pack"
version = "0.1.0"

[resources]
tools = ["tools/"]
skills = ["skills/"]
prompts = ["prompts/"]
agents = ["agents/"]
hooks = ["hooks/"]
```

- `qqcode install <git-url | local-path> [--name X]`: clone/copy vào `~/.qqcode/packages/<name>`, thêm vào config `packages: list[str]`.
- `qqcode list` / `qqcode remove <name>` / `qqcode update <name>` (git pull).
- Loader mở rộng `ToolManager`/`SkillManager`/prompt loaders: thêm resource dir của package vào search paths (sau project, trước global? — thứ tự: project > package > global, ghi rõ trong docs).
- Không có registry trung tâm ở phase này; user cài từ git URL hoặc path.

### 3.6 Ví dụ kèm repo (`examples/hooks/`)

| Ví dụ | Chức năng |
|---|---|
| `protected_paths.py` | deny edit/write vào glob cấu hình (`.env`, `secrets/**`) |
| `audit_log.py` | ghi tool_call/tool_result ra `~/.qqcode/logs/audit.jsonl` |
| `notify_done.py` | gửi desktop notification khi `agent_end` |
| `context_inject.py` | thêm message nhắc style khi `before_request` |

Các ví dụ này viết mới cho QQcode, dùng API trên, có test.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 8.1 | Event models + HookDecision + test | `vibe/core/hooks/events.py` (mới) | M | — |
| 8.2 | HookRegistry + dispatch + timeout | `vibe/core/hooks/registry.py` (mới) | L | 8.1 |
| 8.3 | Loader `.qqcode/hooks`, `~/.qqcode/hooks` | `hooks/loader.py` (mới) | M | 8.2 |
| 8.4 | Tích hợp Agent các điểm gọi (gồm `before/after compact`) | `agent.py`, `context/compactor.py` | L | 8.2 |
| 8.5 | Helper API `on_tool_call`, `deny`, `modify` | `hooks/api.py` (mới) | S | 8.2 |
| 8.6 | Config `hooks` (timeout, enabled, paths) | `config.py` | S | 8.3 |
| 8.7 | Manifest + install/list/remove/update | `vibe/core/packages.py` (mới), `cli/entrypoint.py` | L | — |
| 8.8 | Loader package resources vào Tool/Skill/Prompt/Agent | managers, `config.py` | M | 8.7 |
| 8.9 | 4 ví dụ hook + test | `examples/hooks/`, tests | M | 8.5 |
| 8.10 | Docs `docs/hooks.md`, `docs/packages.md` | docs | M | 8.9 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/hooks/test_registry.py` | priority, deny thắng, modify gộp, handler lỗi bị cô lập |
| `tests/hooks/test_loader.py` | load file, import lỗi không chặn; enabled=false |
| `tests/hooks/test_agent_integration.py` | deny tool → tool result skipped; modify args → tool nhận args mới; inject trước request |
| `tests/hooks/test_compact_hooks.py` | before_compact deny → không compact; summary_override thay LLM; after/compact_failed phát đúng |
| `tests/packages/test_install_local.py` | install từ path, resources xuất hiện trong tool/skill list |
| `tests/packages/test_manifest.py` | manifest thiếu field báo lỗi rõ |
| `tests/test_config_migration.py` | config cũ không có `packages`/`hooks` vẫn chạy |

## 6. DoD

- [ ] Extension hook chặn được bash nguy hiểm trong test end-to-end (`FakeBackend` + agent thật).
- [x] Hook `before_compact` từ chối được compaction và có thể cấp `summary_override`.
- [ ] Hook lỗi/timeout không làm agent dừng; có log.
- [ ] `qqcode install <local>` với manifest copy đúng resource dirs; `remove` gỡ sạch khỏi search paths.
- [ ] Không load hook project khi chưa trust (khi plan 10 merge; trước đó ghi TODO rõ trong code).
- [ ] Tài liệu hooks/packages có ví dụ chạy được.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Hook chạy code tùy ý — rủi ro bảo mật | global hooks là code của user; project hooks gate bởi trust (plan 10); docs cảnh báo rõ |
| Dispatch chậm khi nhiều hook | giới hạn timeout + log; async gather khi nhiều handler (trừ tool_call cần tuần tự) |
| Package xung đột tên tool/skill | ưu tiên project > package > global; log warning khi trùng |
| Hook thay args làm sai schema | validate lại args sau modify (dùng `model_validate`) |

Rollback: `hooks.enabled=false`; package chỉ là search path, xoá config là hết.

## 8. Commit plan

```
feat(hooks): define lifecycle events for extensions
feat(hooks): dispatch tool and agent events with decisions
feat(hooks): let extensions veto or supply compaction summaries
feat(hooks): load project and user hook modules
feat(hooks): add convenience helpers and examples
feat(packages): install resource packs from git or paths
feat(packages): discover tools, skills and prompts from packages
```
