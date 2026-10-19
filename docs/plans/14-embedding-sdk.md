# Plan 14 — Embedding SDK (public programmatic API)

> Gap: C5 · Mức độ: G2×1 · Wave 3

## 1. Mục tiêu

1. API Python public, ổn định để nhúng agent vào ứng dụng khác (không bọc CLI, không copy `run_programmatic`).
2. Một **event contract** có version cho luồng streaming, dùng chung với `--output events` (plan 07) và VSCode/ACP.
3. Điều khiển runtime từ host: `prompt`, `steer`, `abort`, `compact`, `set_model`, session list/resume/fork.
4. Tài liệu `docs/sdk.md` + ví dụ chạy được (`examples/sdk/`), kèm type hints đầy đủ cho pyright.
5. Không phá vỡ `run_programmatic()` hiện có — SDK là lớp mỏng quanh `Agent` + session store.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Chỉ có entry nội bộ cho `-p` | `vibe/core/programmatic.py:144-219` |
| Agent là class nội bộ, không export API | `vibe/core/agent.py`; `vibe/__init__.py` không re-export |
| Event stream `--output vscode` là tự định nghĩa, chưa tài liệu hoá | `vibe/core/output_formatters.py:85-227` |
| ACP là kênh tích hợp duy nhất | `vibe/acp/acp_agent.py` |
| Chưa có package/version surface cho SDK | `pyproject.toml` chỉ export 2 script CLI |

## 3. Thiết kế

### 3.1 Package `vibe.sdk`

```python
# vibe/sdk/__init__.py
from vibe.sdk.agent import AgentSession, create_agent
from vibe.sdk.events import SDKEvent
from vibe.sdk.options import AgentOptions, SessionRef

__all__ = ["create_agent", "AgentSession", "AgentOptions", "SDKEvent", "SessionRef"]
```

```python
# vibe/sdk/options.py
class AgentOptions(BaseModel):
    config_path: Path | None = None
    agent: str | None = None              # ~/.qqcode/agents/<name>.toml
    model_alias: str | None = None
    enabled_tools: list[str] | None = None
    system_prompt: str | None = None
    append_system_prompt: list[str] = []
    auto_approve: bool = False
    working_dir: Path = Path.cwd()
    save_session: bool = True
    session: SessionRef | None = None     # resume/fork
```

### 3.2 `AgentSession` (async context manager)

```python
class AgentSession:
    async def __aenter__(self) -> "AgentSession": ...
    async def __aexit__(self, *exc) -> None: ...

    async def prompt(
        self, text: str, *, attachments: list[Path] = []
    ) -> AsyncIterator[SDKEvent]: ...

    async def steer(self, text: str) -> None: ...
    async def abort(self) -> None: ...
    async def compact(self, instructions: str | None = None) -> None: ...
    async def set_model(self, alias: str) -> None: ...
    async def set_thinking(self, level: str) -> None: ...

    @property
    def session_id(self) -> str: ...
    def stats(self) -> AgentStats: ...
    def messages(self) -> list[LLMMessage]: ...
    async def close(self) -> None: ...
```

- Bên trong bọc `Agent`, adapter `message_observer` → async queue → `AsyncIterator[SDKEvent]`.
- Mỗi session độc lập; nhiều `AgentSession` chạy song song được (mỗi cái có `Agent` + session file riêng).

### 3.3 Event contract (`vibe/sdk/events.py`)

Tái dùng schema versioned của plan 07 (`--output events`) làm nguồn sự thật:

```python
class SDKEvent(BaseModel):
    type: str
    schema_version: Literal[1] = 1
    timestamp: str
    # union theo type: session_started, turn_started, message_delta,
    # tool_call, tool_result, turn_completed, session_compacted, error
```

- Publisher chung `vibe/core/events.py`: một `EventBus` mà CLI formatter, SDK và VSCode đều subscribe. Không có hai định nghĩa event song song.
- `docs/sdk.md` in JSON schema sinh từ model (`SDKEvent.model_json_schema()`) để host validate.

### 3.4 Session API (`SessionRef`)

```python
class SessionRef(BaseModel):
    session_id: str | None = None
    fork_at_entry_id: str | None = None

class create_agent(...) -> AgentSession
```

```python
# ví dụ
async with create_agent(AgentOptions(model_alias="claude-sonnet-4.5")) as s:
    async for ev in s.prompt("summarize README.md"):
        ...
```

- Resume: `AgentOptions(session=SessionRef(session_id="..."))` dùng plan 02 `SessionStore`.
- Fork: `SessionRef(fork_at_entry_id="...")` (plan 02 `fork`).

### 3.5 Tương thích & đóng gói

- `run_programmatic()` cũ chuyển sang dùng `AgentSession` nội bộ (không đổi output/flag) để tránh hai đường code.
- Không thêm dependency mới. Thêm `[project.optional-dependencies] sdk = []` trống như điểm móc tương lai (nếu tách package).
- Version API: `vibe.sdk.__api_version__ = 1`; thay đổi phá vỡ phải tăng và ghi trong CHANGELOG.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 14.1 | `EventBus` chung + union event versioned | `vibe/core/events.py` (mới) | M | plan 07 |
| 14.2 | CLI formatter (`--output events`/`vscode`) subscribe `EventBus` | `output_formatters.py` | M | 14.1 |
| 14.3 | `AgentOptions`, `SessionRef` | `vibe/sdk/options.py` (mới) | S | — |
| 14.4 | `AgentSession` + bridge observer → async iterator | `vibe/sdk/agent.py` (mới) | L | 14.1, 14.3 |
| 14.5 | `steer`/`abort`/`compact`/`set_model` qua session | `sdk/agent.py`, `agent.py` | M | plan 06/07 |
| 14.6 | Session resume/fork qua `SessionStore` | `sdk/agent.py` | M | plan 02 |
| 14.7 | `run_programmatic` refactor dùng `AgentSession` | `core/programmatic.py` | M | 14.4 |
| 14.8 | Docs `docs/sdk.md` + `examples/sdk/01..04` | docs, examples | M | 14.4 |
| 14.9 | `__api_version__` + test ổn định event schema | `sdk/__init__.py`, tests | S | 14.1 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/sdk/test_session_prompt.py` | FakeBackend: `prompt()` phát event đúng thứ tự, kết thúc iterator |
| `tests/sdk/test_session_control.py` | `steer` chèn message; `abort` dừng; `compact` giảm token; `set_model` đổi alias |
| `tests/sdk/test_session_resume.py` | resume session id; fork tại entry; stats khớp |
| `tests/sdk/test_events_schema.py` | schema version; mọi type có trong union; JSON schema sinh ổn định |
| `tests/test_cli_programmatic_preload.py` | refactor `run_programmatic` không đổi output/flag |
| `tests/sdk/test_parallel_sessions.py` | hai `AgentSession` chạy song song không lẫn state |

## 6. DoD

- [x] SDK chạy được ví dụ trong `examples/sdk/` không dựa vào CLI.
- [x] Event schema dùng chung cho SDK và `--output events`; có JSON schema tài liệu hoá.
- [x] Resume/fork qua SDK hoạt động cùng `SessionStore` (plan 02).
- [x] `run_programmatic()` cũ giữ nguyên hành vi (test).
- [x] Type hints đầy đủ, `pyright` xanh; `ruff`, `pytest` xanh.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Hai định nghĩa event (CLI vs SDK) lệch nhau | `EventBus` + union duy nhất trong `core/events.py` |
| Refactor `run_programmatic` phá VSCode/ACP | test hồi quy; giữ facade cũ gọi SDK |
| Session chạy song song tranh tài nguyên (cwd, tool dirs) | mỗi session có instance `Agent` + `ToolManager` riêng; test |
| API đổi gây vỡ host | `__api_version__` + CHANGELOG; deprecate trước khi bỏ |

Rollback: SDK là package mới, không ảnh hưởng CLI; gỡ `vibe.sdk` và trả `run_programmatic` về code cũ.

## 8. Commit plan

```
refactor(events): publish agent events through one bus
feat(sdk): add an async embedding session api
feat(sdk): expose steering, abort and compaction controls
feat(sdk): resume and fork sessions from the sdk
docs(sdk): document the event contract and examples
```
