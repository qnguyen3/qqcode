# 08 — Roadmap & Thứ tự ưu tiên

> Tổng hợp từ các gap trong `01`–`07`. Mục tiêu: chỉ ra **thứ tự làm** dựa trên (a) mức độ ảnh hưởng tới chất lượng agent, (b) phụ thuộc kỹ thuật, (c) chi phí triển khai.

## 0. Nguyên tắc

1. **Context trước provider trước UX**: context engineering (doc 03) quyết định chất lượng agent; provider registry (doc 01) là điều kiện để context hoạt động trên mọi model; UX (doc 02) xếp sau.
2. **Không big-bang**: mỗi thay đổi phải giữ tương thích ngược config/history hiện có (JSON session cũ vẫn đọc được; config.toml cũ vẫn load).
3. **Mỗi hạng mục có "definition of done" đo được** (test hoặc metric).

## 1. Phase 0 — Quick wins (1–2 tuần)

| Việc | Gap | File chạm | DoD |
|---|---|---|---|
| Token estimator cục bộ + kiểm tra trước request | X1 (G3) | thêm `vibe/core/context/tokens.py`; dùng ở `middleware.py` | test đơn vị cho estimator; auto-compact kích hoạt trước khi vượt ngưỡng |
| Truncation helper dùng chung + notice | X11, H5 (G2) | `vibe/core/tools/truncate.py`; sửa `bash.py`, `read_file.py`, `grep.py` | bash giữ **tail** + temp file; mọi output có `[truncated ...]` |
| Parallel tool execution | H2 (G2) | `agent.py:551-751` | nhiều tool call độc lập chạy song song, thứ tự message giữ nguyên; test |
| Timeout chung cho tool | H3 (G1) | `tools/base.py`, `agent.py` | tool treo bị timeout, trả tool result lỗi |
| Tool result có cấu trúc | H4 (G2) | `agent.py:641-651` | result gửi model giữ JSON có field (không `k: v`) |
| Prompt: giữ section, không thay `messages[0]` khi đổi mode | R1, R14 (G2) | `system_prompt.py`, `agent.py:1134-1146` | cache hit system block khi plan↔interactive |
| Skill prompt thêm `location` | E3 (G1) | `skills/manager.py:217-224` | model tự `read` được SKILL.md không cần tool |
| `find`/`ls` tool | H11 (G1) | `tools/builtins/` | 2 tool mới có prompt + test; cập nhật `bash.md` |

## 2. Phase 1 — Context & session foundation (3–5 tuần)

| Việc | Gap | File chạm | DoD |
|---|---|---|---|
| Compact giữ recent window + cut boundary hợp lệ | X2 (G3) | `agent.py:1071-1126` | giữ ~20k token cuối; không tách assistant↔tool_result; test hồi quy |
| Overflow recovery (compact + retry 1 lần) | X4 (G3) | `agent.py:753-865` | lỗi "prompt too long" tự compact + retry; test với FakeBackend |
| Session JSONL append + entry types | S1, S2, X15 (G3) | `interaction_logger.py` | append-only có type/id; reader tương thích file JSON cũ |
| Atomic write / recovery khi resume | S6 (G3) | `interaction_logger.py` | crash giữa ghi không mất toàn bộ session; test cắt file |
| Iterative summary | X5 (G2) | `compact.md` + `agent.py` | compact lần 2 dùng prompt update preserve; test |
| Usage/cost entry + footer/status context % | S13 (G1), X10 | `interaction_logger.py`, `app.py:810` | hiển thị `% context`, cache read/write nếu có |
| Model capability registry + estimator dùng capability | P3, P4 (G3) | `config.py:249-257` | model có `context_window`, `max_output`, `supports_*`; validation trong `VibeConfig` |
| Prompt caching OpenAI-compatible + ổn định system | P7, R14 (G2) | `generic.py:73-128`, `agent.py` | `cache_retention` config; `prompt_cache_key` cho OpenAI; cache hit log |

## 3. Phase 2 — Provider & programmability (3–6 tuần)

| Việc | Gap | File chạm | DoD |
|---|---|---|---|
| Adapter registry + api_style mở rộng (google/bedrock?) | P2, P10 (G2) | `llm/backend/generic.py`, `factory.py` | thêm adapter mới không sửa `GenericBackend` lõi |
| OAuth flow mở rộng (OpenAI/Copilot) + refresh có khoá | P5, P6 (G2) | `core/oauth/` | không refresh trùng; test concurrency |
| Reasoning levels runtime | P8 (G2) | `config.py`, backends | `thinking_level` config + map sang `extra_body`; parse `reasoning_content` OpenAI-style |
| Prompt templates | C6, R2 (G2) | `cli/commands.py`, `vibe/core/prompts/` | file `.md` có `$1/$@` thành slash command; test substitution |
| RPC/JSON mode ổn định + tài liệu schema | C3, C4 (G3) | `programmatic.py`, `output_formatters.py` | lệnh điều khiển qua stdin (compact, abort, set_model); schema tài liệu |
| `/model` trực tiếp + scoped models | C9 (G1) | `textual_ui/app.py`, `config_app.py` | đổi model không cần mở config; scope Ctrl+P |
| Context files đa cấp | R3 (G2) | `system_prompt.py:30-37` | ancestor walk + override + CLAUDE.md; test |
| Fork/clone session tối giản | C7, S3 (G3) | `interaction_logger.py`, `commands.py` | `/fork` tạo file mới cắt từ message chọn; UI chọn message |

## 4. Phase 3 — Ecosystem & durability (dài hạn)

| Việc | Gap | Ghi chú |
|---|---|---|
| Extension/hook API | E1 (G3) | registry + event bus; cho custom tool/agent đăng ký hook; không cần bằng Pi |
| MCP hardening: OAuth, resources, CLI, cache | E7, E8, E9 (G2) | có thể dùng `mcp` Python SDK sẵn có |
| Skills parity + `/skill:name` | E3–E5 (G1) | dễ |
| Resource package (`qqcode install <git>`) | E13 (G3) | manifest + loader |
| Theme/keybindings chia sẻ | E12 (G1) | JSON + reload |
| Subagent tối giản | S7 (G3) | tool `task` spawn Agent con |
| Durable task/commit nền tảng | S6, S15 (G3) | chỉ làm nếu theo đuổi background task/subagent bền vững |
| Telemetry & eval harness | S11, S12 (G2) | bắt đầu bằng eval nội bộ dùng FakeBackend |
| Sandbox/container docs | S10 (G2) | tài liệu + Dockerfile mẫu |

## 5. Dependency graph (rút gọn)

```
Token estimator ──> Compact giữ recent ──> Overflow recovery ──> Auto-compact đáng tin
       │                                          │
       └──> Model capability registry ──> Provider/adapter mở rộng ──> Reasoning levels
Session JSONL append ──> Atomic/recovery ──> Fork/clone ──> Tree/branch summary ──> Subagent
Prompt section + diff ──> Cache ổn định ──> Prompt templates ──> Extension chèn section
Hook API ──> MCP hardening ──> Permission gates mở rộng ──> Package ecosystem
```

## 6. Chỉ số nên theo dõi sau mỗi phase

| Chỉ số | Lấy ở đâu | Mục tiêu |
|---|---|---|
| Tỷ lệ context sử dụng khi compact | `AgentStats.context_tokens` / context limit | compact xảy ra ở ~80–90% thay vì 100% |
| Số tool result bị cắt mà không có notice | log tool | 0 |
| Số lần session hỏng khi resume | log | 0 sau JSONL |
| Cache hit rate (Anthropic/OpenAI) | usage cacheRead | tăng dần |
| Thời gian từ "prompt too long" tới phục hồi | test harness | < 1 vòng compact+retry |
| Số provider/model thêm mới không cần sửa code | git diff | ≥ 1 mỗi phase 2 |

## 7. Kết luận

- QQcode **không cần** trở thành Pi. Điểm mạnh riêng (permission built-in + plan mode + todo + ACP + UI Textual) nên giữ và khai thác.
- Ba khoản đầu tư có ROI cao nhất, xếp theo thứ tự: **(1) token estimator + compact giữ recent + overflow recovery**, **(2) session JSONL append + fork**, **(3) model capability registry + adapter mở rộng**.
- Phần còn lại (RPC, prompt templates, MCP OAuth/resources, extension API, packages, telemetry/evals) là bề mặt cạnh tranh dài hạn, làm sau khi ba khoản trên ổn định.
