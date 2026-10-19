# Plan 05 — Provider Registry: model capabilities, catalog, pricing, caching

> Gap: P1, P3, P4, P6, P7, P11 · Mức độ: G3×2, G2×4 · Wave 2

## 1. Mục tiêu

1. Model có **metadata năng lực** (context window, max output, vision, reasoning, tools, cache) thay vì chỉ `context_limit`.
2. Thêm provider/model mới qua file cấu hình, merge với default, không cần sửa code.
3. Pricing đầy đủ (input/output/cache read/cache write) để cost tracking chính xác.
4. Prompt caching cấu hình được theo provider (`cache_retention`), gồm OpenAI-compatible `prompt_cache_key`.
5. Nhịp cập nhật catalog rõ ràng (local overlay trước; remote refresh là tùy chọn phase sau).

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| `ModelConfig` chỉ có `context_limit`, giá input/output, `extra_body` | `vibe/core/config.py:249-257` |
| `DEFAULT_PROVIDERS`/`DEFAULT_MODELS` hard-code | `config.py:268-427` |
| Merge default chỉ theo alias | `config.py:567-588` |
| Cost thô, ghi chú "worst case" | `vibe/core/types.py:74-89` |
| Cache chỉ có cho Anthropic SDK + Qwen | `llm/backend/anthropic_sdk.py:134-220`; `generic.py:184-185` |
| Không capability; backend tự đoán | `llm/format.py:147-161` |

## 3. Thiết kế

### 3.1 Model metadata (`vibe/core/config.py`)

```python
class ModelCapabilities(BaseModel):
    tools: bool = True
    tool_choice: bool = True
    reasoning: bool = False
    vision: bool = False
    prompt_cache: Literal["none", "short", "long"] = "none"
    parallel_tool_calls: bool = True

class ModelPricing(BaseModel):
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0

class ModelConfig(BaseModel):
    name: str
    provider: str
    alias: str
    temperature: float = 0.2
    context_window: int = 200_000
    max_output_tokens: int | None = None
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    reasoning_level: ThinkingLevel | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)

    # Tương thích ngược
    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_fields(cls, data: Any) -> Any:
        """input_price -> pricing.input_per_million; context_limit -> context_window
        (chỉ khi user thật sự set; giữ đúng ngữ nghĩa cũ)."""
```

- `context_limit` cũ vẫn được đọc bởi `get_context_limit_for_active_model` — cập nhật property dùng `context_window`.
- `AgentStats` thêm `cache_read_tokens`, `cache_write_tokens`; `session_cost` tính theo `ModelPricing`, làm tròn 6 chữ số.
- `update_pricing` nhận `ModelPricing`.

### 3.2 Provider config mở rộng

```python
class ProviderConfig(BaseModel):
    name: str
    api_base: str
    api_key_env_var: str = ""
    api_style: str = "openai"          # sẽ mở rộng ở plan 06
    backend: Backend = Backend.GENERIC
    oauth_token: ... = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    cache_retention: Literal["default", "none", "short", "long"] = "default"
    timeout: float | None = None       # override api_timeout
    models_path: str | None = None     # file model overlay riêng (đường dẫn hoặc URL)
```

### 3.3 Local catalog overlay

- File `~/.qqcode/models.toml` (và `.qqcode/models.toml` project nếu bật):
  ```toml
  [[models]]
  name = "claude-sonnet-5-0"
  provider = "anthropic"
  alias = "claude-sonnet-5"
  context_window = 250000
  max_output_tokens = 64000
  [models.capabilities]
  reasoning = true
  vision = true
  prompt_cache = "long"
  [models.pricing]
  input_per_million = 3.0
  output_per_million = 15.0
  cache_read_per_million = 0.3
  cache_write_per_million = 3.75
  ```
- Loader: `load_model_overlay() -> list[ModelConfig]`; merge theo `alias` (overlay thắng), model mới thì thêm.
- `qqcode models list [--json]` và `qqcode models validate` (kiểm tra trùng alias, capability hợp lệ, provider tồn tại).
- Remote refresh (tùy chọn, phase sau): `qqcode models refresh --source URL` tải file TOML/JSON cùng schema, lưu cache `~/.qqcode/cache/models.json`; không bắt buộc cho plan này.

### 3.4 Capability-aware behavior

Áp dụng capability ở các điểm quyết định:

| Nơi | Dùng capability |
|---|---|
| `get_active_tool_classes` | nếu `capabilities.tools=false`, không gửi tool (chat thuần) |
| `agent._chat*` | gửi `tool_choice="auto"` chỉ khi `tool_choice=true` |
| `select_cut`/threshold | dùng `context_window` + `max_output_tokens` để tính reserve |
| `read_file` (tương lai ảnh) | nếu `vision=false`, trả placeholder text |
| `parallel execution` | nếu `parallel_tool_calls=false`, ép sequential |
| `anthropic_sdk`/`generic` cache | dùng `provider.cache_retention` + `capabilities.prompt_cache` |

### 3.5 Prompt caching (`vibe/core/llm/cache.py`)

```python
@dataclass(frozen=True)
class CachePlan:
    enabled: bool
    ttl: Literal["5m", "1h"] | None
    session_key: str | None        # prompt_cache_key cho OpenAI-compatible

def build_cache_plan(provider: ProviderConfig, model: ModelConfig, session_id: str) -> CachePlan: ...
```
- Anthropic: `cache_control` trên system block cuối + block cuối message áp chót (giữ hành vi hiện có), TTL theo `cache_retention`.
- OpenAI-compatible: gắn `prompt_cache_key = session_id[:64]` khi `cache_retention != none`; gửi thêm `prompt_cache_retention: "24h"` nếu provider hỗ trợ (cấu hình `provider.extra_body` hoặc field `cache_retention="long"`).
- Không áp cache cho request summarizer (truyền `CachePlan(enabled=False)`).
- Log `usage.cache_read_tokens`/`cache_write_tokens` nếu backend trả (Generic: parse từ usage OpenAI; Anthropic: SDK đã có).

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 5.1 | `ModelCapabilities`, `ModelPricing`, `ModelConfig` mới + legacy validator | `vibe/core/config.py` | M | — |
| 5.2 | Cập nhật `DEFAULT_MODELS` sang schema mới (giá trị suy từ dữ liệu cũ) | `config.py` | S | 5.1 |
| 5.3 | `AgentStats` cache tokens + cost theo pricing mới | `vibe/core/types.py` | M | 5.1 |
| 5.4 | Overlay loader + merge + validate | `vibe/core/config.py` hoặc `vibe/core/models_overlay.py` (mới) | M | 5.1 |
| 5.5 | `qqcode models list/validate` CLI | `cli/entrypoint.py` | S | 5.4 |
| 5.6 | Capability-aware tool/candidate decisions | `llm/format.py`, `agent.py` | M | 5.1 |
| 5.7 | `build_cache_plan` + tích hợp Anthropic/Generic | `vibe/core/llm/cache.py` (mới), backends | M | 5.1 |
| 5.8 | Parse cache usage (OpenAI + Anthropic) | backends, `types.py` | S | 5.3 |
| 5.9 | UI: `/status` hiển thị cache tokens + cost mới | `textual_ui/app.py` | S | 5.3 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/core/test_config_migration.py` | `input_price`/`context_limit` cũ map đúng; không có field mới vẫn load |
| `tests/core/test_config_overlay.py` | overlay thắng default theo alias; model mới thêm; alias trùng báo lỗi |
| `tests/backend/test_cache_plan.py` | TTL, session key 64 ký tự, tắt cache cho summarizer |
| `tests/backend/test_usage_parse.py` | cache tokens parse từ response mẫu OpenAI/Anthropic |
| `tests/test_agent_stats.py` | cost với cache pricing; làm tròn |
| `tests/backend/test_backend.py` | capability tools=false không gửi tools |

## 6. DoD

- [ ] `config.toml` cũ chạy không đổi hành vi (test migration).
- [ ] Thêm model mới chỉ bằng `models.toml`, không sửa code; `qqcode models validate` phát hiện lỗi.
- [ ] Cost hiển thị gồm cache read/write khi có dữ liệu.
- [ ] Prompt caching OpenAI-compatible bật qua config, không ảnh hưởng khi `cache_retention="default"` với provider không hỗ trợ.
- [x] `context_window`/`max_output_tokens` được dùng ở plan 01 (threshold) và plan 06 (max_tokens clamp).

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Legacy validator map sai giá | test với cả config.toml thật trong repo tests/backend/data |
| Overlay phá default khi user ghi thiếu field | merge theo field cụ thể; default giữ nguyên |
| Cache TTL "1h" bị provider không hỗ trợ | chỉ gửi khi `capabilities.prompt_cache == "long"` |
| `prompt_cache_key` gây lỗi provider cũ | chỉ gửi khi `provider.cache_retention != default` hoặc `extra_body` yêu cầu |

Rollback: bỏ overlay (không cần file), `cache_retention="none"`; các field mới đều có default.

## 8. Commit plan

```
feat(models): describe model capabilities and pricing in config
feat(models): merge local model overlays over built-in defaults
feat(models): add models list and validate commands
feat(cost): track cache read and write tokens
feat(llm): configure prompt caching per provider
feat(llm): honor model capabilities when building requests
```
