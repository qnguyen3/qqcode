# 01 — Providers & Auth (LLM layer)

> Nguồn đối chiếu:
> - **QQcode** `@ be6a96c` (v1.2.0), Python ~19.9k LOC trong `vibe/` (111 file `.py`).
> - **Pi** `@ 636703a0a`, TS monorepo 16 package, 1.566 file `.ts` / ~380k LOC trong `packages/`.
>
> Mọi khẳng định dưới đây đều kèm `file:line`. QQcode dùng tiền tố `qqcode/`, Pi dùng `pi/`.

## 1. Tóm tắt điều hành

QQcode có **3 backend class** phục vụ **7 provider mặc định** khai báo cứng trong code, với model catalog cũng khai báo cứng (hand-maintained). Pi có **42 provider built-in** (cộng custom provider runtime), **10 API adapter** (chat) + classifier + image, **catalog model sinh tự động** từ models.dev/OpenRouter/Vercel Gateway/Radius, **9 luồng OAuth** với refresh có khoá, prompt caching đa provider, reasoning levels, cost accounting và overflow detection.

Khoảng cách lớn nhất không nằm ở "số provider" mà ở **tầng metadata & vòng đời model**: QQcode không có model capabilities, không có catalog refresh, không có cost tier/cache pricing, không có reasoning control runtime, và không có cơ chế đo context bằng estimate — tất cả đều ảnh hưởng trực tiếp tới context engineering (xem `03-context-engineering.md`).

### Bảng điểm trục Providers & Auth

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| P1 | Số provider built-in | 7 | 42 | G2 |
| P2 | API adapter | 3 (openai/anthropic style + mistral SDK) | 10 chat + 1 image + 3 classifier | G2 |
| P3 | Model catalog động | Không (hard-code) | Sinh từ models.dev/OpenRouter/Gateway/Radius | G3 |
| P4 | Model capabilities metadata | Không | reasoning/thinking map, input limits, promptCache, strict tools | G3 |
| P5 | OAuth flows | 2 (Anthropic PKCE, Qwen device) | 9 (Anthropic, OpenAI, Codex, Copilot, OpenRouter, Kimi, Meta, xAI, Radius) | G2 |
| P6 | Token/cost accounting | Chỉ input/output token + 2 giá | Usage đầy đủ (cacheRead/cacheWrite/reasoning/cost tier) | G2 |
| P7 | Prompt caching | Thủ công cho Qwen + Anthropic SDK | Chuẩn hoá theo 7 họ API | G2 |
| P8 | Reasoning/thinking control | Chỉ qua `extra_body` tĩnh theo alias | 6 level + budget + interleaved + redacted replay | G2 |
| P9 | Retry / rate limit | 3 lần, backoff, 7 status code | Retry-After, cap 60s, agent-level retry, overflow detect | G1 |
| P10 | Custom provider runtime | Qua `config.toml` (khai báo tay) | `createProvider` runtime + extension `registerProvider` | G1 |
| P11 | Đo context tokens | Gọi API thật (`count_tokens`) | Heuristic 3.5 chars/token + usage anchor | G2 |
| P12 | Image input | Không hỗ trợ | `read` tool trả ảnh, downgrade ảnh khi model không vision | G2 |
| P13 | Embeddings | Không | Không | G0 |
| P14 | WebSocket transport / deferred | Không | Codex WS + deferred protocol | G1 |

---

## 2. QQcode hiện có gì (bằng chứng)

### 2.1 Kiến trúc backend 3 lớp

`qqcode/vibe/core/llm/backend/factory.py:8-12`:

```python
BACKEND_FACTORY = {
    Backend.MISTRAL: MistralBackend,
    Backend.GENERIC: GenericBackend,
    Backend.ANTHROPIC: AnthropicBackend,
}
```

`Backend` enum chỉ 3 giá trị (`qqcode/vibe/core/config.py:151-155`): `MISTRAL`, `GENERIC`, `ANTHROPIC`.

- `GenericBackend` (`generic.py:507-832`) tự viết HTTP bằng `httpx`, chọn adapter theo `provider.api_style` (`generic.py:637-638`, `704-705`) trong `BACKEND_ADAPTERS` chỉ có 2 key `"openai"` và `"anthropic"` (`generic.py:53`, `69`, `237`).
- `AnthropicBackend` (`anthropic_sdk.py:280-693`) dùng SDK `anthropic`.
- `MistralBackend` (`mistral.py:114-332`) dùng SDK `mistralai`.

Cả 3 backend đều phải implement `complete`, `complete_streaming`, `count_tokens` (protocol ngầm, không có ABC chung).

### 2.2 Provider & model khai báo cứng

7 provider mặc định (`config.py:268-311`): `mistral`, `llamacpp`, `openrouter`, `anthropic`, `zai`, `qwen`, `xiaomimimo`. Mỗi provider chỉ có: `name`, `api_base`, `api_key_env_var`, `api_style`, `backend`, `oauth_token`, `extra_headers` (`config.py:157-165`).

Model cũng khai báo cứng (`config.py:313-427`), mỗi model chỉ có: `name`, `provider`, `alias`, `temperature`, `input_price`, `output_price`, `context_limit`, `extra_body` (`config.py:249-257`). Ví dụ alias biến thể thinking của Claude được hard-code bằng `extra_body` (`config.py:377-382`):

```python
ModelConfig(
    name="claude-sonnet-4-5-20250929",
    provider="anthropic",
    alias="claude-sonnet-4.5:thinking",
    extra_body={"thinking": {"type": "enabled", "budget_tokens": 2000}},
),
```

Người dùng có thể thêm provider/model trong `config.toml` (`providers`, `models` fields — `config.py:447-450`), nhưng:
- **Không có catalog/refresh**: không có nơi nào fetch danh sách model từ provider. `DEFAULT_MODELS` + merge validator (`config.py:567-588`) là toàn bộ vòng đời.
- **Không có capability flags**: không có `reasoning`, `vision`, `tool_call`, `contextWindow` động, `maxOutput`.
- **Không có cost tier/cache pricing**: chỉ `input_price`/`output_price` (`config.py:254-255`), `AgentStats.session_cost` tính thô (`types.py:74-89`) kèm ghi chú "worst-case scenario... actual cost may be lower due to prompt caching".

### 2.3 Auth

- API key: đọc từ env var theo `provider.api_key_env_var` (`generic.py:588-594`, `anthropic_sdk.py:293-299`, `mistral.py:119-123`).
- OAuth Anthropic PKCE: `oauth/claude.py:12-17` (client id/authorize/token URL/SCOPES), exchange + refresh (`claude.py:64-164`); login flow paste code (`cli/login.py:20-64`).
- OAuth Qwen device flow: `cli/login.py:67-129` (device code + poll).
- Refresh khi hết hạn: `OAuthToken.is_expired()` buffer 10% (`oauth/token.py:16-19`); refresh đường Anthropic SDK (`anthropic_sdk.py:301-326`) và đường Generic (`generic.py:596-622`). **Không có khoá chống race** khi nhiều request refresh song song (Pi dùng double-checked lock — `pi/packages/ai/src/auth/resolve.ts:102-155`).
- Header `x-affinity: session_id` để giữ session affinity với backend (`agent.py:772-775`, `829-832`).
- Header OAuth Claude Code: chèn prefix system prompt `"You are Claude Code..."` (`anthropic_sdk.py:28-29`, `60-62`; `generic.py:400-407`) và `anthropic-beta: oauth-2025-04-20`.

### 2.4 Prompt caching hiện có

- Anthropic SDK: `cache_control: {type: ephemeral}` trên system block cuối (`anthropic_sdk.py:205-220`) + trên block cuối của **message áp chót** (`anthropic_sdk.py:134-203`).
- OpenAI adapter: chỉ bật khi `provider.name == "qwen"` (`generic.py:184-185`), thêm cache_control dạng Anthropic-style vào system + message áp chót (`generic.py:73-128`).
- Không hỗ trợ `prompt_cache_key`/`prompt_cache_retention` của OpenAI, không hỗ trợ Bedrock cache point, không có `cacheRetention` config.

### 2.5 Retry / lỗi / đo tokens

- Retry cố định 3 lần, delay 0.5s * 2^n + jitter nhỏ, chỉ với status `{408, 409, 425, 429, 500, 502, 503, 504}` (`utils.py:171-176`, `async_retry` `utils.py:177-216`, `async_generator_retry` `utils.py:219-264`). Không đọc `Retry-After`.
- `count_tokens` là **API call thật**: gửi messages + `max_tokens=16` (Generic, `generic.py:800-827`) hoặc `max_tokens=1` (Anthropic/Mistral, `anthropic_sdk.py:672-693`, `mistral.py:309-332`) và lấy `usage.prompt_tokens`. Đây là điểm quan trọng vì `Agent.compact()` gọi nó để cập nhật `stats.context_tokens` (`agent.py:1107-1117`).
- Không có phát hiện context overflow theo error string (Pi có `overflow.ts` nhận 30+ pattern — `pi/packages/ai/src/utils/overflow.ts:37-171`).

### 2.6 Reasoning/thinking

- Không có abstraction. Muốn bật thinking phải tạo alias model với `extra_body` tĩnh (`config.py:377-399` cho Anthropic; `335-370` cho OpenRouter GPT effort).
- Anthropic SDK có lấy `thinking_delta` + `signature_delta` khi stream (`anthropic_sdk.py:578-603`) và chèn lại thinking block khi gửi (`anthropic_sdk.py:74-83`), cùng guard `is_compatible_with_thinking` (`anthropic_sdk.py:35-47`). Nhưng không có thinking level runtime, budget theo level, hay interleaved thinking (trừ beta header khi OAuth, `anthropic_sdk.py:425-432`).
- Generic adapter cho OpenAI-style **không parse** `reasoning_content` từ stream (chỉ `content` — `generic.py:435-442` cho Anthropic adapter stream, `208-234` cho OpenAI `parse_response`).

### 2.7 Hệ quả (counterfactual)

- Bỏ `DEFAULT_MODELS` merge (`config.py:567-588`) → người dùng phải tự viết toàn bộ model, app mất model mặc định.
- Nếu một model mới ra (vd Claude mới) → phải sửa code + release; Pi chỉ cần refresh catalog (`pi/packages/coding-agent/docs/models.md:15`).
- `count_tokens` gọi API thật → mỗi lần compact tốn thêm 1 request tính tiền; Pi estimate cục bộ rồi neo vào usage cuối (`pi/packages/ai/src/utils/estimate.ts:97-112`).

---

## 3. Pi có gì (bằng chứng)

### 3.1 Số lượng & cấu trúc

- 42 provider built-in được dựng trong `pi/packages/ai/src/providers/all.ts:137-180`; union `KnownProvider` cùng danh sách `pi/packages/ai/src/types.ts:39-81`.
- 10 API chat (`KnownApi`, `types.ts:17-27`): `anthropic-messages`, `openai-completions`, `openai-responses`, `openai-codex-responses`, `azure-openai-responses`, `google-generative-ai`, `google-vertex`, `mistral-conversations`, `bedrock-converse-stream`, `pi-messages`; thêm `openrouter-images` (image) và 3 classifier (`types.ts:31-35`).
- Ví dụ provider đa API: `opencode` hỗ trợ 4 API (`pi/packages/ai/src/providers/opencode.ts:19-26`); `github-copilot` 3 API + OAuth subscription (`providers/github-copilot.ts:9-33`).

### 3.2 Model catalog sinh tự động

- Script `pi/packages/ai/scripts/generate-models.ts` fetch: models.dev `api.json` (`:1770-1772`), OpenRouter 3 listing (`:1311-1325`), Vercel AI Gateway (`:1362`), Radius `/v1/config` (`:1341-1348`), rồi enrich capabilities (strict tools, promptCache, image limits, thinking maps — `:3480-3484`, `:970-1020`).
- Sinh ra `models.generated.ts` + `providers/<id>.models.ts` + data JSON schema v6 (`scripts/model-data.ts:5-15`).
- Runtime: `createModels()` với dynamic refresh, generation guard, abort (`pi/packages/ai/src/models.ts:485-611`).
- Model type đầy đủ: `reasoning`, `thinkingLevelMap`, `promptCache`, `contextWindow`, `maxTokens`, `samplingParams`, `compat` (`types.ts:1113-1146`); cost tier `input/output/cacheRead/cacheWrite` (`types.ts:1058-1073`).

### 3.3 Auth

- 9 luồng OAuth trong `pi/packages/ai/src/auth/oauth/`: anthropic, openai-chatgpt, openai-codex, github-copilot, openrouter, kimi-coding, meta, xai, radius (`load.ts:14-24`).
- 7 provider `isSubscription: true` (anthropic, openai, openai-codex, github-copilot, kimi-coding, meta, xai).
- Refresh có khoá double-checked, min validity 5 phút, timeout 15s, persist token xoay vòng kể cả khi abort (`auth/resolve.ts:102-191`); `Models.refresh()` refresh trước khi gọi mạng (`models.ts:613-640`).
- Credential store abstraction (`auth/types.ts:65-94`).
- AWS chain/ADC: `providers/amazon-bedrock.ts:54-79`, `providers/google-vertex.ts:64-89`.

### 3.4 Prompt caching

- Chuẩn hoá `CacheRetention = "none" | "short" | "long"` (`types.ts:112-118`) và `cacheRetention`/`sessionId` trong options (`types.ts:216-226`).
- Anthropic: cache_control trên system, last tool, last user/tool-result; TTL 1h (`api/anthropic-messages.ts:1483-1509`, `1608`).
- OpenAI Responses: `prompt_cache_key`, `prompt_cache_retention: "24h"`, hoặc `prompt_cache_options` cho GPT-5.6+ (`api/openai-responses.ts:97-114`, `330-336`).
- OpenAI-compatible: `prompt_cache_key` + Anthropic-style markers cho OpenRouter Anthropic models (`api/openai-completions.ts:822-831`, `1077-1191`, `1642`).
- Bedrock cache points + TTL 1h (`api/bedrock-converse-stream.ts:873-931`); Mistral `promptCacheKey` (`api/mistral-conversations.ts:532-538`).
- Cost cache write 1h tính gấp 2 lần input (`models.ts:1209-1215`).

### 3.5 Reasoning / retry / overflow / đo tokens

- 6 mức `minimal|low|medium|high|xhigh|max` + `off`, budgets mặc định (`types.ts:84-109`, `api/simple-options.ts:67-107`), clamp theo model (`models.ts:1220-1252`).
- Anthropic adaptive thinking, interleaved, redacted thinking replay (`api/anthropic-messages.ts:231-291`, `713-722`, `1108-1115`); OpenAI 10 `thinkingFormat` (`types.ts:812-826`); Google thinkingLevel/budget/thought signatures (`api/google-shared.ts:36-160`).
- Retry wrapper tôn trọng `retry-after`, cap 60s (`utils/provider-retry.ts:97-125`); agent-level retry phân loại retryable (`utils/retry.ts:191-257`).
- Overflow detection 30+ pattern + silent overflow (`utils/overflow.ts:37-171`).
- Token estimate `CHARS_PER_TOKEN = 3.5`, ảnh 4800 chars, neo vào usage cuối (`utils/estimate.ts:15-16`, `97-112`).

---

## 4. Bảng gap chi tiết (kèm bằng chứng)

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| Provider registry runtime | tĩnh (`DEFAULT_PROVIDERS`) | `createProvider()` publish transactional, dynamic overlay | `config.py:268-311`, `554-565` | `models.ts:994-1180`, `providers/all.ts:137-180` | G2 |
| Custom provider không cần release | Có qua `config.toml` nhưng phải tự khai báo mọi model | Có `models.json`/extension `registerProvider` + OpenAI-compatible auto-detect | `config.py:447-450`; `README.md:239-261` | `providers/openai-completions.ts:1593-1690`; `extensions/types.ts:1821-1837` | G1 |
| Model catalog refresh | Không | `pi update --models`, offline cache | không có code tương ứng | `docs/models.md:15`; `models.ts:485-611` | G3 |
| Capabilities (vision/reasoning/tool strict) | Không | Có flags + compat per model | `config.py:249-257` | `types.ts:1113-1146` | G3 |
| Context window / max output metadata | 1 số `context_limit` tay | `contextWindow` + `maxTokens` + tiered pricing | `config.py:256` | `types.ts:1058-1073` | G2 |
| Cost accounting cache | Không | cacheRead/cacheWrite/cacheWrite1h + service tier | `types.py:74-89` ghi chú thô | `types.ts:429-450`, `models.ts:1198-1218` | G2 |
| OAuth subscription | 2 provider (anthropic, qwen) | 7 provider + refresh locked | `config.py:600-621`; `cli/login.py` | `auth/oauth/load.ts:14-24`; `auth/resolve.ts:102-191` | G2 |
| OAuth refresh race | Có thể gọi trùng | Khóa `credentials.modify` + min validity | `generic.py:596-622` | `auth/resolve.ts:102-155` | G2 |
| Prompt cache OpenAI | Không | `prompt_cache_key`, retention 24h, options | `generic.py:184-185` (chỉ qwen) | `api/openai-responses.ts:97-114` | G2 |
| Prompt cache system-prompt ổn định | System prompt bị viết lại toàn bộ khi đổi mode | Section diff, chỉ append patch | `agent.py:1134-1146` | `system-prompt.ts:217-229` | G2 |
| Thinking level runtime | Không (chỉ alias tĩnh) | 6 level + per-model map | `config.py:377-399` | `types.ts:84-109`; `simple-options.ts:67-107` | G2 |
| Reasoning stream parse (OpenAI-style) | Không parse `reasoning_content` | Parse 3 field + replay `reasoning_details` | `generic.py:208-234` | `api/openai-completions.ts:607-681` | G2 |
| Retry-After / 429 chuẩn | Không | Có, cap 60s | `utils.py:171-176` | `utils/provider-retry.ts:37-67` | G1 |
| Overflow detection | Không | 30+ pattern + recoverable length | không có | `utils/overflow.ts:37-181` | G2 |
| Đo token | API call thật | estimate + usage anchor | `generic.py:800-827`; `anthropic_sdk.py:672-693` | `utils/estimate.ts:97-112` | G2 |
| Image input | Không | `read` trả ảnh + resize theo limits | `types.py:160-193` (Content chỉ text) | `tools/read.ts:129-152`; `types.ts:413-417` | G2 |
| Image generation | Không | OpenRouter images | không có | `api/openrouter-images.ts:41-198` | G1 |
| Structured output/grammar tools | Không | strict JSON schema + Lark/regex grammar | `base.py:238-258` (JSON schema thường) | `api/constrained-sampling.ts:15-298` | G2 |
| Deferred responses | Không | Protocol + poll phase (durable) | không có | `types.ts:243-252`; `durable/src/harness/generation.ts:429-448` | G1 |
| WebSocket transport | Không | Codex WS cached context | không có | `api/openai-codex-responses.ts:294-383` | G1 |

---

## 5. Ưu tiên đề xuất cho trục này

1. **P0 — Model capability registry + estimate** (G3): thay `count_tokens` API probe bằng estimator cục bộ; thêm field `context_window`, `max_output`, `supports_reasoning`, `supports_images`, `supports_tools` vào `ModelConfig`. Đây là nền cho auto-compact chính xác (doc 03) và điều kiện để thêm provider mới mà không sửa logic.
2. **P1 — Mở rộng provider không cần release** (G2): cho phép `api_style: "openai" | "anthropic" | "mistral" | "google" | "bedrock"` với adapter registry; thêm `extra_headers`/`compat` per provider; bổ sung OAuth flow cho provider phổ biến (OpenAI/Copilot).
3. **P2 — Prompt caching toàn cục** (G2): chuẩn hoá `cache_retention` + `prompt_cache_key` cho OpenAI-compatible; cache system prompt ổn định khi đổi mode (không thay `messages[0]` toàn bộ).
4. **P3 — Reasoning levels** (G2): abstraction `ThinkingLevel` map sang `extra_body` per api_style; parse `reasoning_content` trong OpenAI adapter.

> **Counterfactual**: nếu giữ nguyên model catalog tĩnh, mọi model/provider mới đều là một PR code + release binary; nếu thay bằng registry + estimate, auto-compact và cost tracking sẽ chính xác hơn mà không cần gọi API phụ.
