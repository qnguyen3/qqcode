# Plan 12 — Context Store: edits, head marker, delta read

> Gap: X7, X8, X9 · Mức độ: G3×1, G2×2 · Wave 2

## 1. Mục tiêu

1. **Context dựng từ entry log** (plan 02) thay vì giữ một `list[LLMMessage]` biến đổi tại chỗ trong `Agent`.
2. **Head marker**: `reset`/`/clear` đặt marker mới để model bỏ qua quá khứ, giữ nguyên log + usage + cost, thay vì xoá sạch messages.
3. **Context edits**: entry `omit`/`replace` loại hoặc thay một message khỏi context mà không phá log gốc.
4. **Delta read**: chỉ dựng lại phần messages thay đổi kể từ lần đọc trước, tránh replay O(n) mỗi turn khi history dài.
5. Giữ tương thích: session `.json` legacy, `.jsonl` hiện có và luồng compact của plan 01 vẫn chạy.

**Ngoài phạm vi**: tree/branch đầy đủ và branch summarization (plan 02/11); chỉ làm nền entry + read path cho linear log.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| History là list biến đổi trực tiếp, không derive từ log | `vibe/core/agent.py` `self._messages`; `_conversation_loop` |
| `clear_history` chỉ giữ `messages[:1]` (system), mất ngữ cảnh không phục hồi | `vibe/core/agent.py:1051-1069` |
| Compact thay toàn bộ list bằng `[system, summary]` | `vibe/core/agent.py:1100-1102` |
| Entry log chưa có `context_edit`/`head_marker` | `vibe/core/sessions/entries.py:60-107` (chỉ session/message/compaction/model_change/thinking_level_change/usage/custom) |
| Không có bước replay/derive context từ `SessionView` | `vibe/core/sessions/` không có `ContextBuilder` |

## 3. Thiết kế

### 3.1 Entry mở rộng (`vibe/core/sessions/entries.py`)

```python
class HeadMarkerEntry(SessionEntryBase):
    type: Literal["head_marker"] = "head_marker"
    keep_from_id: str | None      # None = chỉ giữ entry sau marker
    reason: str | None = None     # "reset" | "clear" | "compact"

class ContextEditEntry(SessionEntryBase):
    type: Literal["context_edit"] = "context_edit"
    target_id: str                # entry bị sửa (message/compaction)
    action: Literal["omit", "replace"] = "omit"
    replacement: str | None = None  # chỉ dùng khi action="replace" (nội dung thay thế)

SessionEntry = Annotated[
    SessionHeader | MessageEntry | CompactionEntry | ModelChangeEntry
    | ThinkingLevelChangeEntry | UsageEntry | CustomEntry
    | HeadMarkerEntry | ContextEditEntry,
    Field(discriminator="type"),
]
```

Quy ước:
- `HeadMarkerEntry`: context bắt đầu từ entry **sau** marker gần nhất (bỏ mọi message/compaction trước). Marker mới nhất thắng; marker cũ không xoá.
- `ContextEditEntry` áp theo thứ tự xuất hiện; edit sau ghi đè edit trước trên cùng `target_id`.
- Cả hai loại này **không** ảnh hưởng stats/cost (usage/compaction vẫn giữ).

### 3.2 ContextBuilder (`vibe/core/context/builder.py`)

```python
@dataclass(frozen=True)
class BuildResult:
    messages: list[LLMMessage]
    last_entry_id: str | None
    reused_from: str | None      # entry id mốc cache tái dùng

class ContextBuilder:
    def __init__(self, system_message: LLMMessage) -> None: ...

    def build(self, entries: list[SessionEntry]) -> BuildResult:
        """Dựng messages: head marker → replay message → áp context_edit
        → pressure compaction (first_kept_id) → bọc summary message.
        Bất biến: không để lại tool_call mồ côi (tái dùng
        _fill_missing_tool_responses trước khi trả)."""

    def build_from(self, entries: list[SessionEntry], base: BuildResult) -> BuildResult:
        """Delta: nếu base.last_entry_id còn trong entries và không có edit/marker
        mới trỏ về trước nó → tiếp tục append từ base; ngược lại gọi build()."""
```

### 3.3 Delta read / range cache

- `Agent` giữ `self._context_base: BuildResult | None`.
- Mỗi turn: `needed = store.view()` (đọc thêm entry mới từ file bằng cách theo dõi offset/vị trí đọc cuối — `SessionView` bổ sung `read_since(entry_id)`), rồi `builder.build_from(entries, self._context_base)`.
- Vô hiệu cache khi: có `HeadMarkerEntry` mới, `ContextEditEntry` mới có `target_id` ≤ `base.last_entry_id`, hoặc `CompactionEntry` mới. Các trường hợp này gọi `build()` toàn phần.
- Chỉ số đo: số entry replay lại mỗi turn ≈ số entry mới, không tăng theo độ dài hội thoại (test 200 entry).

### 3.4 Tích hợp Agent

- Thay `self._messages` bằng `self._context.messages` derive từ `ContextBuilder`; mọi chỗ append message đi qua `SessionRecorder` (plan 02) rồi cập nhật `_context_base` bằng delta.
- `clear_history()` → ghi `HeadMarkerEntry(reason="clear")` + giữ system; context rebuild = `[system]`.
- `compact()` (plan 01) → ghi `CompactionEntry` như cũ; builder đọc `first_kept_id` để cắt.
- `Agent.reset_context()` là API nội bộ dùng bởi `/clear` và plan 14 SDK.

### 3.5 UI / command

| Command | Hành vi |
|---|---|
| `/clear` (đã có) | đặt head marker thay vì xoá list; hiển thị "context cleared, log preserved" |
| `/context` (mới) | liệt kê entry gần đây + số message omitted/replaced + head marker hiện tại |
| `/omit <index>` / `/replace` | tùy chọn giai đoạn sau; trước mắt chỉ dùng nội bộ + extension hook (plan 08) |

> Ghi chú: đưa `/omit`/`/replace` ra UI là tùy chọn; phần bắt buộc là entry model + builder + delta read.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 12.1 | `HeadMarkerEntry`, `ContextEditEntry` + union + test roundtrip | `vibe/core/sessions/entries.py` | M | plan 02 |
| 12.2 | `ContextBuilder.build` (head marker + edit + compaction replay) | `vibe/core/context/builder.py` (mới) | L | 12.1 |
| 12.3 | Delta `build_from` + range cache | `builder.py`, `sessions/store.py` (`read_since`) | L | 12.2 |
| 12.4 | Tích hợp Agent: derive context, bỏ list biến đổi | `agent.py`, `sessions/recorder.py` | L | 12.3 |
| 12.5 | `clear_history`/`reset_context` ghi head marker | `agent.py` | S | 12.4 |
| 12.6 | `/context` + cập nhật `/clear` | `cli/textual_ui/app.py`, `commands.py` | S | 12.4 |
| 12.7 | Legacy: session `.json`/`.jsonl` không có entry mới vẫn build đúng | `sessions/migrate.py`, `builder.py` | M | 12.2 |
| 12.8 | Hook `context_edit` trước build (móc cho plan 08, tùy chọn) | `hooks/`, `builder.py` | S | plan 08 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/context/test_builder_replay.py` | head marker cắt đúng; compaction `first_kept_id`; không còn tool_call mồ côi |
| `tests/context/test_context_edit.py` | omit xoá 1 message; replace thay nội dung; edit sau thắng; edit nhắm entry trước marker bị bỏ qua |
| `tests/context/test_delta_read.py` | 200 entry: số message replay mỗi turn chỉ bằng entry mới; invalidate khi có marker/edit/compaction |
| `tests/sessions/test_store_read_since.py` | `read_since` trả đúng phần mới, file truncated an toàn |
| `tests/test_agent_reset_context.py` | `/clear` ghi head marker, log + usage + cost giữ nguyên, context = `[system]` |
| `tests/sessions/test_legacy.py` | session cũ không có entry mới build ra cùng messages như trước |

## 6. DoD

- [x] `Agent` không còn giữ list messages biến đổi làm nguồn sự thật; context derive từ entry log (grep không còn gán trực tiếp `self._messages = [...]`).
- [x] `/clear` không mất log: resume được, stats/cost giữ nguyên.
- [x] `omit`/`replace` thay đổi context nhưng file `.jsonl` không bị ghi đè.
- [x] Delta read: số entry replay mỗi turn độc lập với độ dài hội thoại (test 200 entry).
- [x] Session `.json` legacy và `.jsonl` hiện có đọc ra cùng context như trước; `ruff`, `pyright`, `pytest` xanh.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Delta cache sai → gửi context lệch | bất biến invalidate rõ ràng; test so `build` full vs `build_from` bằng nhau |
| Edit tạo context không hợp lệ (tool_call mồ côi) | `_fill_missing_tool_responses` chạy sau mọi build; test |
| Head marker làm model mất ngữ cảnh cần thiết | marker ghi rõ `reason`; `/context` cho người dùng thấy phần bị bỏ |
| Replay chậm khi history rất lớn lần đầu | một lần full build đầu session là chấp nhận được; sau đó delta |

Rollback: config `context.entry_derived: bool = false` quay về giữ list trực tiếp (builder chỉ dùng để migrate/experiment).

## 8. Commit plan

```
feat(sessions): record head markers and context edits
feat(context): build request messages from the session log
feat(context): apply omit and replace edits when building context
perf(context): reuse the previous context range when reading new entries
feat(cli): keep history on clear by adding a head marker
```
