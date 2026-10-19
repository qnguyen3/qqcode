# Plan 01 — Context Engineering v2

> Gap: X1, X2, X3, X4, X5, X6, X10, X12 · Mức độ: G3×3, G2×5 · Wave 1

## 1. Mục tiêu

1. Biết trước khi nào context sắp tràn — không cần gọi API để đo.
2. Compaction giữ lại các lượt gần nhất, không mất chi tiết tool call đang làm dở.
3. Tự phục hồi khi provider báo tràn context thay vì chết session.
4. Cập nhật summary dần theo thời gian thay vì tóm tắt lại từ đầu.
5. Hiển thị mức sử dụng context rõ ràng cho người dùng.

**Ngoài phạm vi**: context edit omit/replace, head marker, durable commit (để dành cho plan 02/11).

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| `context_tokens` chỉ có sau request, từ `usage` | `vibe/core/agent.py:790-792`, `858-860` |
| Ngưỡng compact = full context window, không reserve | `vibe/core/middleware.py:87-107`; `vibe/core/config.py:513-521` |
| Compact thay toàn bộ history bằng `[system, summary]` | `vibe/core/agent.py:1100-1102` |
| Đo lại context sau compact bằng 1 API call thật | `vibe/core/agent.py:1107-1117`; `vibe/core/llm/backend/generic.py:800-827` |
| Không phát hiện overflow | không có code liên quan (verified bằng grep `overflow|too long|context_length`) |
| Cảnh báo 50% mặc định tắt | `vibe/core/config.py:436` |
| Prompt `compact.md` là one-shot 7 mục | `vibe/core/prompts/compact.md:1-48` |

## 3. Thiết kế

### 3.1 Estimator (`vibe/core/context/tokens.py`)

```python
CHARS_PER_TOKEN = 3.5
IMAGE_CHARS = 4800  # tương lai khi có ảnh; hiện giữ hằng số để ổn định

def estimate_text_tokens(text: str | None) -> int: ...

def estimate_message_tokens(message: LLMMessage) -> int:
    """Nội dung + tool_calls JSON + overhead role/name."""

def estimate_tool_tokens(tools: list[AvailableTool]) -> int:
    """JSON schema + description của từng tool."""

def estimate_context_tokens(
    messages: list[LLMMessage],
    tools: list[AvailableTool],
    last_usage: LLMUsage | None = None,
) -> int:
    """Neo vào usage của assistant cuối: usage.prompt_tokens + ước lượng các
    message sau nó + tool schema. Nếu chưa có usage: ước lượng toàn bộ."""
```

Quy tắc:
- `usage.prompt_tokens` đã tính cả system + tools tại thời điểm request → chỉ cộng phần messages phát sinh **sau** assistant message tương ứng.
- Cần biết index của message chứa usage: thêm `AgentStats.last_usage_message_index: int`.
- Sai số chấp nhận: ±15% so với API; mục tiêu chỉ để kích hoạt compact sớm.

### 3.2 Ngưỡng & config (`vibe/core/config.py`)

Thêm `CompactionConfig`:

```python
class CompactionConfig(BaseSettings):
    enabled: bool = True
    reserve_tokens: int = 16_384     # chừa chỗ cho response + system tiếp theo
    keep_recent_tokens: int = 20_000 # giữ nguyên văn phần đuôi
    threshold_ratio: float = 0.90    # fallback khi model chưa có context_window
```

Ngưỡng kích hoạt:
```
threshold = context_window - reserve_tokens      # nếu biết context_window
          = context_limit * threshold_ratio      # fallback
```

`ContextWarningMiddleware` đổi ngưỡng thành hai mức `0.70` và `0.90`, mặc định `context_warnings = True`.

### 3.3 Cut selection (`vibe/core/context/cut.py`)

```python
@dataclass(frozen=True)
class CutResult:
    first_kept_index: int      # message đầu tiên giữ nguyên văn
    is_split_turn: bool        # cắt vào giữa một turn (assistant đã gọi tool)

def select_cut(messages: list[LLMMessage], keep_recent_tokens: int) -> CutResult:
    """Đi ngược từ cuối, cộng dồn token tới keep_recent_tokens,
    dừng tại ranh giới hợp lệ:
      - ưu tiên message user (bắt đầu turn);
      - không cắt giữa assistant có tool_calls và các message tool phía sau;
      - nếu điểm dừng rơi vào giữa turn → đánh dấu split_turn và lùi về
        assistant gần nhất để summary mô tả trọn tool call.
    Bất biến: first_kept_index nằm trên biên không tách tool_call ↔ tool_result.
    """
```

Bất biến kiểm thử: với mọi history sinh hợp lệ bởi Agent, đoạn `messages[:first_kept_index]` khi kết thúc bằng assistant có tool_calls thì bắt buộc có đủ tool results phía sau bị cắt — hoặc cut được lùi thêm.

### 3.4 Compactor (`vibe/core/context/compactor.py`)

Chuyển logic ra khỏi `Agent`:

```python
class Compactor:
    def __init__(self, agent: "Agent") -> None: ...

    async def compact(
        self,
        *,
        instructions: str | None = None,   # cho /compact [ghi chú]
        force: bool = False,
    ) -> CompactionResult: ...

@dataclass
class CompactionResult:
    summary: str
    cut: CutResult
    tokens_before: int
    tokens_after_estimate: int
    usage: LLMUsage | None
    is_update: bool                    # đã dùng prompt cập nhật summary cũ
```

Luồng:

```
1. _clean_message_history()
2. cut = select_cut(messages[1:], keep_recent_tokens)     # bỏ system
3. serialized = serialize_for_summary(messages[1:1+cut.first_kept_index])
   - tool result cắt mỗi đoạn tối đa 2000 chars, ghi rõ "[truncated]"
4. system prompt riêng cho summarizer (không dùng prompt CLI):
   "You summarize conversations. Do not continue the conversation."
   + prompt tóm tắt (mục 3.5), kèm instructions của user nếu có
5. gọi _chat() với prompt tóm tắt, cache write tắt nếu backend hỗ trợ
6. messages = [system] + [summary message] + messages[1+cut.first_kept_index:]
7. stats.context_tokens = estimate_context_tokens(...)   # KHÔNG gọi API
8. ghi session entry "compaction" (plan 02)
```

Message summary: `LLMMessage(role=User, content="<conversation_summary>\n...\n</conversation_summary>")` — giữ dạng user để tương thích mọi backend hiện tại, nhưng bọc tag để model phân biệt.

### 3.5 Prompt tóm tắt (`vibe/core/prompts/summary.md` + `summary_update.md`)

Viết mới cho QQcode, cấu trúc bắt buộc (7 mục giữ tinh thần `compact.md` nhưng gọn và có luật preserve):

```
## Goal
## Constraints & Preferences
## Progress (Done / In Progress / Blocked)
## Key Decisions
## Next Steps
## Critical Context
```
Luật: preserve exact file paths, function names, error messages; không bịa;
nếu thiếu thông tin ghi "(none)". Prompt update nhận `<previous_summary>` và
yêu cầu giữ nguyên thông tin cũ, chỉ bổ sung/điều chỉnh mục Progress/Next Steps.

### 3.6 Overflow recovery (`vibe/core/context/overflow.py`)

```python
OVERFLOW_PATTERNS = (
    "context length", "context window", "maximum context", "prompt is too long",
    "too many tokens", "input is too long", "reduce the length",
    "context_length_exceeded", "max_tokens", "token limit",
)

def is_context_overflow(error: BaseException) -> bool:
    """Nhận BackendError/HTTPStatusError/RuntimeError; kiểm tra status 400/413/422
    + body/parsed message khớp pattern (case-insensitive)."""
```

Tích hợp trong `Agent._conversation_loop`:

```python
except Exception as exc:
    if is_context_overflow(exc) and not self._overflow_recovered:
        self._overflow_recovered = True
        await self._handle_middleware_result(MiddlewareResult(action=COMPACT))
        async for event in self._perform_llm_turn(): yield event
        # không raise; đánh dấu turn đã phục hồi
```

- `_overflow_recovered` reset mỗi khi có user message mới.
- Nếu compact cũng lỗi → raise kèm thông báo rõ ràng cho người dùng.

### 3.7 UI context usage

- `AgentStats.context_percent` + `context_window`; `/status` hiển thị `% (tokens/window)`.
- Widget `context_progress` (đã có) nhận `context_percent` qua event mới `ContextUsageEvent` phát sau mỗi turn.
- Cảnh báo middleware chèn `vibe_warning` tại 70% và 90%, mỗi mức chỉ một lần/turn.

## 4. Task breakdown

| # | Task | File chính | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 1.1 | Estimator + unit test | `vibe/core/context/tokens.py` (mới) | M | — |
| 1.2 | `AgentStats` thêm `context_window`, `context_percent`, `last_usage_message_index` | `vibe/core/types.py` | S | — |
| 1.3 | `CompactionConfig` + `context_warnings=True` + migration config | `vibe/core/config.py` | S | — |
| 1.4 | `select_cut` + bất biến test | `vibe/core/context/cut.py` (mới) | L | 1.1 |
| 1.5 | `Compactor` (recent window) + tích hợp Agent | `vibe/core/context/compactor.py`, `agent.py` | L | 1.4 |
| 1.6 | Prompt `summary.md`, `summary_update.md` + test prompt render | `vibe/core/prompts/` | S | 1.5 |
| 1.7 | Iterative summary (giữ `previous_summary` trong session state) | `compactor.py`, `agent.py` | M | 1.6 |
| 1.8 | Overflow classifier + recovery loop | `vibe/core/context/overflow.py` (mới), `agent.py` | M | 1.5 |
| 1.9 | Middleware ngưỡng mới + warning 2 mức + event usage | `middleware.py`, `types.py`, `textual_ui/` | M | 1.2, 1.3 |
| 1.10 | `/compact [instructions]` truyền ghi chú vào prompt | `cli/textual_ui/app.py`, `commands.py` | S | 1.5 |
| 1.11 | Xoá `count_tokens` khỏi luồng compact; giữ API cho tương thích | `agent.py`, backends | S | 1.5 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/context/test_tokens.py` | token tăng đơn điệu; usage anchor khớp; tool schema được tính |
| `tests/context/test_cut.py` | không tách assistant↔tool; giữ ≥ keep_recent; split turn fallback; history rỗng/1 message |
| `tests/context/test_compaction_v2.py` | FakeBackend: summary thay đúng đoạn, recent messages còn nguyên; `tokens_after_estimate < tokens_before` |
| `tests/context/test_compaction_update.py` | compact lần 2 dùng prompt update và giữ dữ liệu cũ |
| `tests/context/test_overflow.py` | pattern khớp 400/413/422; recovery chạy 1 lần; recovery thất bại báo lỗi rõ |
| `tests/test_agent_auto_compact.py` | cập nhật theo hành vi mới (kỳ vọng recent window) |
| `tests/test_agent_context_warning.py` | 70%/90% chỉ cảnh báo một lần |

## 6. DoD

- [ ] Auto-compact kích hoạt trước khi request vượt `context_window - reserve_tokens` (test với FakeBackend).
- [ ] Sau compact, `messages` chứa system + summary + phần đuôi ≥ keep_recent (trừ khi history ngắn hơn).
- [ ] Compact không còn gọi `count_tokens` (test spy backend).
- [ ] Overflow recovery phục hồi thành công trong test mô phỏng lỗi.
- [ ] `/status` hiển thị `% context`; warning 70/90 hoạt động.
- [ ] Config cũ không có `[compaction]` vẫn chạy với default.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Estimator lệch nhiều với model mới | neo usage + threshold_ratio 0.90; log chênh lệch estimate vs usage thật để tinh chỉnh |
| Cut sai gây API lỗi (tool_call mồ côi) | bất biến trong `select_cut` + validator `_fill_missing_tool_responses` chạy trước cut |
| Recovery loop vô hạn | cờ `_overflow_recovered` per user turn; test |
| Prompt summary mới kém hơn prompt cũ | giữ `compact.md` làm fallback qua config `compaction.prompt = "summary" | "legacy"` |

Rollback: config `compaction.enabled=false` quay về hành vi cũ; các module mới không ảnh hưởng khi tắt.

## 8. Commit plan

```
feat(context): add local token estimator for conversations
feat(context): configure compaction reserve and recent-window sizes
feat(context): keep recent turns when summarizing conversation history
feat(context): update existing summaries incrementally
feat(context): recover from provider context overflow
feat(context): warn at 70 and 90 percent context usage
feat(ui): show context usage percentage in status
```
