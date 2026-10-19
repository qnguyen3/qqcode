# Plan 02 — Session Store: JSONL append, recovery, fork

> Gap: S1, S2, S3, S4, S6, X15 · Mức độ: G3×4, G2×2 · Wave 1

## 1. Mục tiêu

1. Ghi hội thoại **tăng dần** thay vì ghi đè toàn bộ file JSON mỗi event.
2. Session có cấu trúc entry (message, compaction, model_change, usage...) làm nền cho compact, cost tracking, fork.
3. Crash giữa ghi không mất cả session; resume cắt tới entry hợp lệ cuối.
4. `--continue`, `--resume`, `--list-sessions`, `--get-session` giữ nguyên hành vi; file JSON cũ vẫn đọc được.
5. Fork session từ một entry bất kỳ (nền cho `/fork` trong plan 07).

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| `save_interaction` ghi đè toàn bộ JSON | `vibe/core/interaction_logger.py:155-204` |
| Gọi lưu rất thường xuyên (mỗi turn/tool/lỗi) | `vibe/core/agent.py:273-275`, `346-371`, `1074-1076`, `1120-1122` |
| Không entry type; chỉ `metadata` + `messages` | `interaction_logger.py:183-194` |
| Không atomic; crash giữa ghi có thể hỏng JSON | `interaction_logger.py:199-200` |
| Resume quét mtime/id | `interaction_logger.py:235-275` |
| Compact reset session id → file mới, không nối tiếp | `vibe/core/agent.py:1119` |

## 3. Thiết kế

### 3.1 Entry schema (`vibe/core/sessions/entries.py`)

Mỗi dòng JSONL là một entry có header chung:

```python
class SessionEntryBase(BaseModel):
    type: str
    id: str                    # ULID/uuid7 string, tăng theo thời gian
    parent_id: str | None = None
    timestamp: str             # ISO 8601

class SessionHeader(SessionEntryBase):
    type: Literal["session"] = "session"
    version: int = 1
    cwd: str
    git_branch: str | None = None
    git_commit: str | None = None
    username: str | None = None

class MessageEntry(SessionEntryBase):
    type: Literal["message"] = "message"
    message: LLMMessage

class CompactionEntry(SessionEntryBase):
    type: Literal["compaction"] = "compaction"
    first_kept_id: str | None      # entry id của message giữ lại đầu tiên
    summary: str
    tokens_before: int
    tokens_after: int
    is_update: bool = False

class ModelChangeEntry(SessionEntryBase):
    type: Literal["model_change"] = "model_change"
    model_alias: str
    provider: str

class UsageEntry(SessionEntryBase):
    type: Literal["usage"] = "usage"
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

class CustomEntry(SessionEntryBase):
    type: Literal["custom"] = "custom"
    key: str
    value: dict[str, Any]

SessionEntry = Annotated[
    SessionHeader | MessageEntry | CompactionEntry | ModelChangeEntry | UsageEntry | CustomEntry,
    Field(discriminator="type"),
]
```

Quy ước `parent_id`: entry mới trỏ tới entry trước đó trong **cùng nhánh** (linear log giai đoạn này; tree đầy đủ để plan 11). `first_kept_id` cho phép replay context: `messages = messages(header..) với compaction cắt theo first_kept_id`.

### 3.2 Store (`vibe/core/sessions/store.py`)

```python
class SessionStore(Protocol):
    def append(self, entry: SessionEntry) -> None: ...
    def read(self, path: Path) -> SessionView: ...
    def list(self, *, workdir: Path | None, limit: int) -> list[SessionSummary]: ...
    def find(self, session_id: str) -> Path | None: ...
    def fork(self, source: Path, at_entry_id: str, *, workdir: Path) -> Path: ...

class JsonlSessionStore:
    """Append-only. Mỗi append: json.dumps + "\n" + flush + fsync (cấu hình được).
    Thư mục: ~/.qqcode/sessions/<project-slug>-<hash8>/<timestamp>_<id8>.jsonl"""
```

- `SessionView`:
  ```python
  @dataclass
  class SessionView:
      header: SessionHeader
      entries: list[SessionEntry]
      def messages(self, *, include_compacted: bool = False) -> list[LLMMessage]
      def stats(self) -> AgentStats            # từ UsageEntry
      def last_model(self) -> ModelChangeEntry | None
      def fork_point(self, parent_id: str | None) -> SessionView
  ```
- `messages()` replay: đi từ đầu, gặp `CompactionEntry` thì bỏ mọi message trước `first_kept_id` và chèn message summary (role user, tag `<conversation_summary>`).
- Ghi qua `aiofiles` (giữ dependency sẵn có) nhưng `append` có thể sync trong thread executor; bật/tắt `fsync` bằng `session_logging.fsync: bool = True`.

### 3.3 Tương thích & migrate (`vibe/core/sessions/migrate.py`)

- `read_legacy_json(path) -> SessionView`: đọc format cũ (`metadata`, `messages`) và map sang entry list trên bộ nhớ.
- `InteractionLogger` trở thành **facade**: `save_interaction(...)` được thay bằng `append_*`; để tránh sửa lan rộng, giai đoạn đầu facade phát hiện entry mới → ghi JSONL; `load_session` hỗ trợ cả `.json` và `.jsonl`.
- Lệnh `qqcode sessions migrate [--dir DIR]`: chuyển `.json` cũ sang `.jsonl` (giữ file gốc, thêm hậu tố `.migrated`). Không bắt buộc chạy.
- `find_session_by_id` nhận cả hai đuôi.

### 3.4 Recovery

- `read()` bỏ qua (log warning) dòng JSON hỏng ở **cuối file**; nếu hỏng ở giữa → dừng và trả entry tới trước dòng hỏng, đánh dấu `SessionView.truncated_at`.
- Resume với file truncated: tiếp tục ghi append bình thường sau khi `qqcode sessions repair <id>` (cắt file tại dòng hỏng, tạo backup `.bak`). Không tự sửa để tránh mất dữ liệu.
- Append dùng pattern: ghi 1 dòng hoàn chỉnh trong một `write()`; trước khi ghi, `os.replace` không cần vì append-only; fsync định kỳ (mỗi entry nếu `fsync=true`).

### 3.5 Fork

```python
def fork(source: Path, at_entry_id: str, *, workdir: Path) -> Path:
    """Tạo file .jsonl mới chứa: header mới (id mới, parent_session=source id)
    + toàn bộ entry từ đầu tới at_entry_id (giữ id cũ), rồi nối tiếp từ đó."""
```
- Thêm field `parent_session_id: str | None` trong `SessionHeader`.
- `Agent` nhận `session_id` mới; `--resume` file fork hoạt động như session bình thường.
- Không copy usage/stats của tương lai (chỉ tới điểm fork).

### 3.6 Tích hợp Agent

Thay các chỗ gọi `save_interaction` bằng API mới, gom vào một helper:

```python
class SessionRecorder:
    def on_message_added(self, message: LLMMessage, parent_id: str | None) -> None
    def on_usage(self, usage: LLMUsage, cost: float) -> None
    def on_model_change(self, alias: str, provider: str) -> None
    def on_compaction(self, result: CompactionResult) -> None
```
- `Agent` giữ `self._recorder`; message_observer hiện tại (VSCode/VScodeJsonFormatter) độc lập, không đổi.
- Ghi đồng bộ ngay sau khi append message để đảm bảo crash giữa turn vẫn còn dữ liệu tới turn trước.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 2.1 | Entry models + discriminated union + test roundtrip | `vibe/core/sessions/entries.py` (mới) | M | — |
| 2.2 | `JsonlSessionStore.append/read/list/find` | `vibe/core/sessions/store.py` (mới) | L | 2.1 |
| 2.3 | `SessionView` + replay compaction + stats | `store.py` | M | 2.2 |
| 2.4 | Recovery dòng hỏng + lệnh `sessions repair` | `store.py`, `cli/entrypoint.py` | M | 2.2 |
| 2.5 | Legacy reader + facade ở `InteractionLogger` | `vibe/core/sessions/migrate.py`, `interaction_logger.py` | M | 2.3 |
| 2.6 | `SessionRecorder` + thay call site trong Agent | `vibe/core/sessions/recorder.py`, `agent.py` | M | 2.5 |
| 2.7 | `fork()` + `parent_session_id` | `store.py` | M | 2.3 |
| 2.8 | Lệnh `qqcode sessions list/migrate/repair` | `cli/entrypoint.py` (hoặc `cli/commands.py`) | S | 2.4, 2.5 |
| 2.9 | Giữ `--continue/--resume/--list-sessions/--get-session`; hỗ trợ `.jsonl` | `entrypoint.py` | S | 2.5 |
| 2.10 | Ghi UsageEntry mỗi turn (chuẩn bị cho plan 10) | `recorder.py`, `agent.py` | S | 2.6 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/sessions/test_entries.py` | parse/serialize từng loại; union discriminator; version field |
| `tests/sessions/test_store_append.py` | append nhiều entry, đọc lại đúng thứ tự; parent_id đúng |
| `tests/sessions/test_store_recovery.py` | file cụt giữa dòng; dòng hỏng cuối; read không ném exception |
| `tests/sessions/test_replay.py` | compaction replay đúng `first_kept_id`; messages khớp history runtime |
| `tests/sessions/test_legacy.py` | đọc file JSON cũ, `load_session` tương thích; migrate giữ nguyên messages |
| `tests/sessions/test_fork.py` | fork tại entry giữa; usage tương lai không lẫn; resume fork |
| `tests/test_cli_programmatic_preload.py` | cập nhật theo store mới nhưng hành vi không đổi |
| `tests/sessions/test_recorder_agent.py` | Agent ghi message/usage/model change đúng thời điểm |

## 6. DoD

- [ ] Không còn ghi đè toàn bộ file session trong luồng thường (grep không còn call `save_interaction` rewrite; facade chỉ dùng cho file cũ).
- [ ] Crash mô phỏng (cắt file giữa dòng) → resume đọc được tới entry cuối hợp lệ.
- [ ] `--continue` và `--resume` chạy trên cả session `.jsonl` mới lẫn `.json` cũ.
- [ ] Fork tại entry chỉ định, resume bình thường; có test.
- [ ] `pytest`, `ruff`, `pyright` xanh; kích thước file session tăng tuyến tính theo số entry (kiểm tra bằng test ghi 100 entry).

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| fsync mỗi entry chậm trên ổ đĩa chậm | config `fsync=false` mặc định ngoài CI; bật cho test crash |
| File JSONL bị hỏng giữa file | repair + backup; không tự động truncate |
| Code cũ gọi `InteractionLogger.save_interaction` với signature cũ | facade giữ nguyên signature, chỉ đổi phần ghi |
| Session cũ không có entry id | legacy reader sinh id ổn định (hash nội dung) để fork vẫn dùng được |

Rollback: config `session_logging.format = "jsonl" | "json"`; default `"jsonl"`, đặt `"json"` để quay lại hành vi cũ.

## 8. Commit plan

```
feat(sessions): define typed conversation log entries
feat(sessions): append session entries to a jsonl file
feat(sessions): replay compacted conversations from log entries
fix(sessions): resume from logs truncated by a crash
feat(sessions): fork a session from any entry
feat(cli): add sessions list, migrate and repair commands
feat(sessions): record token usage entries per turn
```
