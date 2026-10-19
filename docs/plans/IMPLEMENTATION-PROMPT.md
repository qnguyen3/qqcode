# PROMPT — Hoàn thiện các phần chưa code (QQcode)

> Copy toàn bộ file này làm prompt khởi đầu cho agent thực thi.

---

Bạn là kỹ sư Python làm việc trong repo `/home/toan/build/qqcode` (package `vibe/`, Python 3.12, quản lý bằng `uv`). Nhiệm vụ của bạn: **triển khai các phần còn thiếu** của bộ plan cải tiến đã có sẵn trong `docs/plans/`. Làm từng hạng mục một, có test, tôn trọng tương thích ngược và quy ước của repo.

## 0. Đọc trước khi làm (bắt buộc)

1. `AGENTS.md` — quy ước code: Python 3.12, `match-case`, walrus khi hợp lý, ít lồng (guard clause), type hints hiện đại (`list`/`dict`, `X | None`), pydantic-first, `pathlib`, **không** inline `# type: ignore`/`# noqa`. Luôn chạy bằng `uv`, không dùng `python`/`pip` trực tiếp.
2. `docs/plans/00-master-plan.md` — nguyên tắc thực thi, bản đồ plan→gap, wave & dependency, **Định nghĩa Done chung (§4.3)**, format commit (§4.2).
3. Plan chi tiết của hạng mục bạn chuẩn bị làm (mục 5 dưới đây).

## 1. Bối cảnh & trạng thái hiện tại

Audit mới nhất (hãy **tự xác minh lại** bằng grep/đọc code trước khi bắt đầu, vì có thể đã thay đổi):

- **Đã xong trọn vẹn:** plan 01, 02, 03, 04, 06, 07, 10, 11 (có implementation + test).
- **Gần xong, còn mảng nhỏ:**
  - `05-provider-registry.md`: thiếu clamp `max_tokens` theo `max_output_tokens`.
  - `08-extensibility-hooks-packages.md`: thiếu lifecycle hook cho compaction (gap **X13**).
  - `09-mcp-hardening.md`: exposure lệch — `hidden` không gọi được theo tên; deferred có thể vĩnh viễn thay vì one-shot.
- **Chưa code gì (6 plan):** 12, 13, 14, 15, 16, 17.

## 2. Định nghĩa "xong" cho mọi việc

Một hạng mục chỉ được coi là xong khi:

- [ ] Code + docstring, tuân thủ `AGENTS.md`; không comment thừa.
- [ ] Test mới xanh; **test cũ không đổi hành vi**.
- [ ] Gate xanh:
  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run pytest
  ```
- [ ] Config cũ (`config.toml`), session cũ (`.json`/`.jsonl`), CLI flag cũ vẫn hoạt động (thêm test migration nếu format đổi).
- [ ] Cập nhật `README.md` / `docs/` nếu là tính năng người dùng.
- [ ] Commit theo Conventional Commits, mô tả **hành vi QQcode**, dùng ngôn ngữ QQcode (`Agent`, `BaseTool`, `middleware`, ...). **Không** nhắc tên dự án tham chiếu, **không** dùng "port/adapted from", **không** dán link ngoài.
- [ ] Tick `- [ ]` → `- [x]` cho các mục DoD tương ứng trong file plan.
- [ ] Cập nhật `docs/plans/00-master-plan.md` nếu trạng thái/độ phủ thay đổi.

## 3. Quy tắc kỹ thuật

- **Được phép copy/port code** từ dự án tham chiếu (pi, MIT) theo quyết định của chủ repo. Giữ attribution trong `THIRD_PARTY_NOTICES.md`; không nhắc nguồn trong commit message hay source code.
- **Không phá vỡ tương thích**: mọi thay đổi format có reader tương thích hoặc lệnh migrate.
- **Mỗi task = một nhánh/commit nhỏ**, không gộp nhiều task lớn vào một PR.
- Nếu phát hiện phải lệch khỏi plan: **cập nhật plan trong cùng PR với code** để tài liệu không lệch thực tế.
- Không sửa test hiện có trừ khi hành vi được chủ đích thay đổi (và ghi rõ lý do).
- Nếu một hạng mục bị chặn (thiếu dependency, quyết định thiết kế mơ hồ): **dừng lại, nêu rõ lý do và đề xuất**, không đoán bừa.

## 4. Thứ tự thực hiện (theo ROI/độ rủi ro)

| # | Hạng mục | Plan | Ước lượng |
|---|---|---|---|
| A | Compact lifecycle hooks (X13) | 08 | S |
| B | Clamp `max_tokens` | 05 | S |
| C | Sửa exposure MCP | 09 | S |
| D | Keybindings tuỳ biến (C10) | 15 | M |
| E | Image input (P12, H17) | 13 | M |
| F | Deferred tool exposure + `tool_search` (H15) | 16 | M |
| G | Context store: edits/head marker/delta (X7, X8, X9) | 12 | L |
| H | Embedding SDK (C5) | 14 | L |
| I | Sandboxed tool scripting (H14) | 17 | L |

Làm xong A→C (vá nhỏ) trước, rồi D→F, rồi G→I. Không bắt đầu G trước khi plan 02 (session store) ổn định (đã xong).

## 5. Chi tiết từng hạng mục

### A — Plan 08 · X13: Compact lifecycle hooks (S)
- Đọc: `docs/plans/08-extensibility-hooks-packages.md` (§3.1, §3.3, §6).
- Việc:
  - Thêm `BeforeCompactEvent`, `AfterCompactEvent`, `CompactFailedEvent` vào `vibe/core/hooks/events.py`.
  - Thêm `summary_override: str | None` vào `HookDecision`.
  - Dispatch `before_compact` (deny → bỏ qua compact; modify + `summary_override` → dùng summary do hook cấp, không gọi LLM), `after_compact`, `compact_failed` trong `vibe/core/context/compactor.py` (và điểm gọi trong `agent.py` nếu cần).
  - Test `tests/hooks/test_compact_hooks.py`.
- DoD: hook deny được compaction; `summary_override` thay LLM; hook lỗi/timeout không làm chết agent.
- Commit: `feat(hooks): let extensions veto or supply compaction summaries`

### B — Plan 05 · Clamp `max_tokens` (S)
- Việc: khi model có `max_output_tokens`, `max_tokens` gửi request = `min(yêu cầu, max_output_tokens)`; giữ nguyên khi không có.
- Test: model có/không có `max_output_tokens`.
- Commit: `fix(llm): clamp max_tokens to the model output limit`

### C — Plan 09 · Sửa exposure MCP (S)
- Việc:
  - `tool_exposure="hidden"`: vẫn đăng ký tool để **gọi được theo tên**, nhưng không expose schema cho model (khớp `docs/mcp.md`).
  - Deferred activation: làm rõ one-shot (tự hết sau request kế tiếp) hoặc cập nhật plan/docs nếu giữ hành vi persistent — nhưng phải nhất quán code ↔ test ↔ docs.
- Test: cập nhật `tests/mcp/test_exposure.py`.
- Commit: `fix(mcp): keep hidden tools callable by name`

### D — Plan 15 · Keybindings tuỳ biến (C10) (M)
- Đọc: `docs/plans/15-keybindings.md`.
- Deliverables: `KeybindingsConfig` (config.py); `vibe/cli/keybindings.py` (action registry + `build_bindings`); áp động vào `app.py`; load global/project + merge ưu tiên; `/hotkeys` (+ `edit`); reload nóng qua `watchfiles`; cảnh báo xung đột; `docs/keybindings.md`.
- DoD theo §6 plan.
- Commit: `feat(cli): load keybindings from a config file`

### E — Plan 13 · Image input (P12, H17) (M)
- Đọc: `docs/plans/13-image-input.md`.
- Deliverables: `TextPart`/`ImagePart` + `Content` parts (`types.py`, giữ tương thích string); `read_file` nhánh ảnh + `ImageRef`; encode ở adapter OpenAI/Anthropic; gate theo `capabilities.vision` (downgrade placeholder); `vibe/core/images.py` (Pillow optional); session lưu ref (không base64); estimator `IMAGE_CHARS`; UI renderer.
- Commit: `feat(core): model text and image message content`

### F — Plan 16 · Deferred tool exposure + `tool_search` (H15) (M)
- Đọc: `docs/plans/16-deferred-tool-exposure.md`.
- Deliverables: `exposure` ClassVar + config override; `ToolRegistry` direct/deferred/expose (one-shot); BM25 `vibe/core/tools/search.py`; builtin `tool_search` + prompt; auto-defer MCP theo ngưỡng; đo schema tokens.
- Commit: `feat(tools): expose deferred tools on demand`

### G — Plan 12 · Context store: edits/head marker/delta (X7,X8,X9) (L)
- Đọc: `docs/plans/12-context-store-edits.md`.
- Deliverables: `HeadMarkerEntry`, `ContextEditEntry` (entries.py); `vibe/core/context/builder.py` (`build` + `build_from` delta); `read_since` ở store; tích hợp Agent (context derive từ log); `/clear` ghi head marker; `/context`.
- Bất biến: không để lại tool_call mồ côi; delta invalidate đúng.
- Commit: `feat(context): build request messages from the session log`

### H — Plan 14 · Embedding SDK (C5) (L)
- Đọc: `docs/plans/14-embedding-sdk.md`.
- Deliverables: `vibe/core/events.py` (`EventBus` chung, schema versioned dùng chung với `--output events`); `vibe/sdk/{options,agent,events}.py` (`create_agent`, `AgentSession` async, `prompt/steer/abort/compact/set_model`, resume/fork); refactor `run_programmatic` dùng SDK; `docs/sdk.md` + `examples/sdk/`.
- Commit: `feat(sdk): add an async embedding session api`

### I — Plan 17 · Sandboxed tool scripting (H14) (L)
- Đọc: `docs/plans/17-tool-scripting.md`.
- Deliverables: `vibe/core/tools/script_sandbox.py` + worker cô lập (restricted builtins, no import/open/net, rlimit); bridge JSON-RPC về `ToolManager`; tool `run_tools_script` + prompt; permission/audit cho từng tool call trong script; config `[tools.script]` (off mặc định).
- Commit: `feat(tools): run tool scripts in an isolated worker`

## 6. Cách báo cáo

Sau mỗi hạng mục, trả về ngắn gọn:
- Task đã xong (theo bảng Task breakdown của plan).
- File đã đổi/tạo.
- Test đã thêm và kết quả gate.
- Commit (message + hash).
- Mục DoD đã tick.
- Nếu bị chặn: lý do + đề xuất, không tự ý mở rộng phạm vi.
