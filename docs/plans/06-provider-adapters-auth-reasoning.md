# Plan 06 — Provider Adapters, Auth & Reasoning

> Gap: P2, P5, P8, P9, P10, P14 · Mức độ: G2×5, G1×1 · Wave 2

## 1. Mục tiêu

1. Adapter registry cho `api_style`: thêm API mới không sửa `GenericBackend`.
2. OAuth mở rộng: registry flow + refresh có khoá, persist an toàn.
3. Reasoning levels runtime (`off/minimal/low/medium/high`) map theo api_style.
4. Parse `reasoning_content` cho OpenAI-compatible stream.
5. Retry tôn trọng `Retry-After`.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Adapter dict 2 phần tử, gắn cứng trong `generic.py` | `vibe/core/llm/backend/generic.py:53`, `69`, `237`, `637-638` |
| `api_style` chỉ `openai`/`anthropic` | `vibe/core/config.py:161`; backends dùng `BACKEND_ADAPTERS[api_style]` |
| OAuth 2 provider, refresh không khoá | `vibe/core/oauth/claude.py`, `qwen.py`; gọi tại `generic.py:596-622`, `anthropic_sdk.py:301-326` |
| Thinking chỉ qua `extra_body` tĩnh | `config.py:377-399` |
| OpenAI adapter không parse `reasoning_content` | `generic.py:208-234`, `431-504` |
| Retry bỏ qua `Retry-After` | `vibe/core/utils.py:171-176`, `177-216` |

## 3. Thiết kế

### 3.1 Adapter registry (`vibe/core/llm/backend/adapters/`)

Tách adapter khỏi `generic.py`:

```
vibe/core/llm/backend/adapters/
├── __init__.py        # registry: register_adapter(name), get_adapter(name)
├── base.py            # APIAdapter protocol (giữ chữ ký hiện tại)
├── openai_chat.py     # chuyển từ OpenAIAdapter
├── anthropic_messages.py  # chuyển từ AnthropicAdapter
└── openai_responses.py    # adapter mới (nếu chọn làm trong plan này)
```

```python
ADAPTER_REGISTRY: dict[str, type[APIAdapter]] = {}

def register_adapter(name: str) -> Callable[[type[T]], type[T]]: ...
def get_adapter(api_style: str) -> APIAdapter: ...
```

- Giữ tên cũ `"openai"`, `"anthropic"` để tương thích config.
- Thêm `"openai-responses"` (dịch `messages` sang input items; hỗ trợ `prompt_cache_key`) — tùy chọn, có thể cắt sang plan sau nếu scope lớn.
- `GenericBackend` chỉ còn: auth, HTTP, retry, dispatch adapter; adapter có hook `postprocess_response`.
- `BackendErrorBuilder` không đổi.

### 3.2 OAuth registry + refresh khoá

```
vibe/core/oauth/
├── base.py        # OAuthFlow protocol: name, is_subscription, login(), refresh(token)
├── registry.py    # register_flow(name, flow); get_flow(name)
├── claude.py      # chuyển flow hiện có
├── qwen.py        # chuyển flow hiện có
└── openai.py      # (tùy chọn phase 2b) device/browser flow
```

- `ProviderConfig.oauth_token` giữ nguyên; thêm `oauth_flow: str = ""` để chỉ định flow (mặc định suy từ provider name).
- Refresh có khoá:
  ```python
  self._refresh_locks: dict[str, asyncio.Lock] = {}
  async def _ensure_fresh_token(self) -> None:
      lock = self._refresh_locks.setdefault(self._provider.name, asyncio.Lock())
      async with lock:
          if not token.is_expired():
              return
          new = await flow.refresh(token.refresh_token)
          self._provider.oauth_token = new
          save_oauth_token(self._provider.name, new)   # atomic write
  ```
- `save_oauth_token` đổi sang ghi `tmp + os.replace` để tránh hỏng config khi crash.
- Clock skew: coi token hết hạn trước 60s (`is_expired(skew=60)`).

### 3.3 Reasoning levels

```python
class ThinkingLevel(StrEnum):
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

DEFAULT_BUDGETS = {MINIMAL: 1024, LOW: 2048, MEDIUM: 8192, HIGH: 16384}
```

- `ModelConfig.reasoning_level` (default None) + override runtime qua session (`Agent.set_thinking_level`).
- Adapter chọn payload:
  - `anthropic`: `{"thinking": {"type": "enabled", "budget_tokens": N}}`; OFF → bỏ thinking; temperature bỏ khi thinking bật (giữ hành vi `anthropic_sdk.py:433-435`).
  - `openai` (chat): `{"reasoning_effort": level}` hoặc `extra_body` override nếu model dùng format khác (zai/qwen/... đã có `extra_body` — giữ nguyên ưu tiên `extra_body` của user).
  - `mistral`: `prompt_mode` hoặc bỏ qua nếu model không hỗ trợ.
- Effective level = `session override` > `model.reasoning_level` > `None` (không gửi).
- Clamp: nếu model `capabilities.reasoning=false`, bỏ qua và log debug.

### 3.4 Parse reasoning OpenAI-compatible

Trong `OpenAIAdapter.parse_response`, đọc các field theo thứ tự ưu tiên:
`reasoning_content` → `reasoning` → `reasoning_text`.
- Gán vào `LLMMessage.reasoning_content`; giữ `content` như cũ.
- Stream: nếu delta có `reasoning_content`, phát chunk với `reasoning_content` (Agent đã xử lý `chunk.message.reasoning_content`, `agent.py:441-448`).
- Với model `openai` thật (Responses API) không nằm trong adapter này — không thay đổi.

### 3.5 Retry tôn trọng Retry-After

`utils.async_retry` / `async_generator_retry` thêm tham số `get_retry_after: Callable[[Exception], float | None]`:

```python
def _retry_after_seconds(exc: Exception) -> float | None:
    if isinstance(exc, httpx.HTTPStatusError):
        raw = exc.response.headers.get("retry-after")
        ...parse int giây hoặc HTTP-date...
        return min(delay, 60.0)
```
- Delay = `max(backoff, retry_after)` nhưng cap 60s; nếu server yêu cầu > 60s → dừng retry và raise (tránh treo UI).
- Không đổi default tries=3.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 6.1 | Tách adapter base + registry; giữ import cũ | `llm/backend/adapters/` (mới) | M | plan 05 |
| 6.2 | Chuyển OpenAI/Anthropic adapter sang module mới | `adapters/openai_chat.py`, `anthropic_messages.py` | M | 6.1 |
| 6.3 | `GenericBackend` dùng registry | `generic.py` | M | 6.2 |
| 6.4 | (Tùy chọn) adapter `openai-responses` | `adapters/openai_responses.py` | L | 6.1 |
| 6.5 | OAuth base + registry + chuyển claude/qwen | `oauth/` | M | — |
| 6.6 | Refresh lock + atomic save token | backends, `config.py:814+` | M | 6.5 |
| 6.7 | `ThinkingLevel` + budgets + adapter mapping | `config.py`, adapters | M | 6.2 |
| 6.8 | Runtime thinking override (`/thinking` để plan 07 gọi) | `agent.py` | S | 6.7 |
| 6.9 | Parse reasoning_content (sync + stream) | `adapters/openai_chat.py` | S | 6.2 |
| 6.10 | Retry-After support | `utils.py` | S | — |
| 6.11 | Test concurrency refresh (2 request đồng thời) | tests | M | 6.6 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/backend/test_adapter_registry.py` | đăng ký/đọc adapter; api_style lạ báo lỗi rõ |
| `tests/backend/test_backend.py` | giữ nguyên hành vi openai/anthropic sau refactor |
| `tests/core/test_oauth_refresh.py` | 2 coroutine refresh đồng thời chỉ gọi API 1 lần; token persist |
| `tests/core/test_oauth_save_atomic.py` | crash mô phỏng giữa ghi không hỏng config |
| `tests/backend/test_thinking.py` | map level → payload Anthropic/OpenAI; extra_body user thắng |
| `tests/backend/test_reasoning_parse.py` | `reasoning_content` sync + stream |
| `tests/test_agent_backend.py` | retry-after 429 được tôn trọng (respx) |

## 6. DoD

- [ ] `generic.py` không còn biết chi tiết adapter cụ thể; thêm adapter mới chỉ động 1 file + register.
- [ ] `api_style` cũ vẫn chạy; config không đổi.
- [ ] Refresh đồng thời không gọi trùng (test).
- [ ] `thinking_level` chạy với Anthropic và ít nhất một provider OpenAI-compatible; OFF tắt hẳn.
- [ ] Reasoning content stream hiển thị trong UI (nếu UI đã hỗ trợ `reasoning_content`).
- [ ] Retry-after được áp dụng, cap 60s.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Refactor adapter làm lệch payload | test backend hiện có (tests/backend/data) chạy nguyên trạng |
| Thinking làm hỏng request với model không hỗ trợ | gate bằng capability + extra_body user |
| Retry-after quá dài treo | cap 60s; vượt cap raise ngay |
| OAuth lock giữ qua event loop mới | lock tạo per backend instance, không global |

Rollback: `api_style`/`oauth_flow` cấu hình được; `thinking_level=None` là mặc định; retry-after có thể tắt bằng config `retry.honor_retry_after=false`.

## 8. Commit plan

```
refactor(llm): split api adapters behind a registry
feat(llm): add an openai responses adapter
refactor(oauth): register provider login flows
fix(oauth): refresh tokens under a per-provider lock
feat(config): add a session thinking level
feat(llm): stream reasoning content from openai-compatible apis
fix(llm): respect retry-after headers on rate limits
```
