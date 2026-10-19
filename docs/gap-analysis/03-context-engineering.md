# 03 — Context Engineering

> Nguồn: **QQcode** `@ be6a96c` · **Pi** `@ 636703a0a`. Bằng chứng dạng `file:line`.

## 1. Tóm tắt điều hành

Đây là trục **yếu nhất và quan trọng nhất** của QQcode. QQcode quản lý context bằng một middleware duy nhất: khi `stats.context_tokens >= context_limit` thì gọi LLM tóm tắt **toàn bộ** lịch sử và thay bằng `[system, summary]` (`agent.py:1071-1126`). Điểm mạnh: đơn giản, prompt tóm tắt chi tiết (`compact.md`). Điểm yếu mang tính kiến trúc:

1. **Đo token bằng cách gọi API thật** (`count_tokens`) và `context_tokens` lấy từ usage của request trước — không có estimator, nên ngưỡng compact chỉ được kiểm tra sau khi request đã chạy.
2. **Không giữ cửa sổ recent** — compact là all-or-nothing, mất toàn bộ chi tiết tool calls gần nhất (chỉ giữ summary + last user message).
3. **Không xử lý overflow thật** (context overflow do provider trả về không được phát hiện/khôi phục), chỉ ngưỡng chủ động.
4. **Không có cấu trúc context history** (head marker, omit/replace, delta read, fork) — muốn làm session tree/branch summary (doc 02, 07) cần thiết kế lại.
5. **Tool output bị cắt theo byte thô** (`[:max_bytes]`) không có line/byte kép, không temp-file spill, không thông báo cho model biết đã mất dữ liệu.

Pi có 3 tầng: agent core không compaction (caller tự hook), `coding-agent` có compaction production (blocking/background/overflow + iterative + split-turn + branch summary), và `durable` có context engine dựa trên entry/head-marker/edits/delta read với crash recovery.

### Bảng điểm trục Context Engineering

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| X1 | Token estimate cục bộ | Không (API probe) | 3.5 chars/token + usage anchor | G3 |
| X2 | Giữ recent window khi compact | Không | keepRecentTokens=20k, cut boundary hợp lệ | G3 |
| X3 | Background compaction | Không | Có (backgroundTokens=32k) | G2 |
| X4 | Overflow recovery | Không | Blocking compact + retry một lần | G3 |
| X5 | Summary iterative (cập nhật dần) | Không (1 phát) | Có, preserve previous + merge | G2 |
| X6 | Split-turn / branch summary | Không | Có | G2 |
| X7 | Context edits (omit/replace) | Không | Có | G3 |
| X8 | Head marker / reset context | Chỉ xoá sạch khi compact | `pi.reset` + head marker | G2 |
| X9 | Delta/incremental context read | Không | `readContextFrom` + scheduler cache | G2 |
| X10 | Cảnh báo context | Middleware 50% (default off) | Footer % + auto/warning, `/session` | G1 |
| X11 | Tool output truncation | byte-cut đơn (`[:max_bytes]`) | line+byte, head/tail, temp file, notice | G2 |
| X12 | Compaction thủ công | `/compact` | `/compact [instructions]`, RPC `compact` | G1 |
| X13 | Hook can thiệp trước compact | Không | `beforeCompact` (durable), `session_before_compact` | G2 |
| X14 | Compaction prompt có cấu trúc | Có (7 mục) | Có (7 mục + iterative + split) | G1 |
| X15 | Crash recovery context | Không | Durable commit/restore | G3 |

---

## 2. QQcode hiện có gì

### 2.1 Nguồn context lúc dựng system prompt

System prompt được dựng **một lần** lúc khởi tạo Agent (`agent.py:147-151`) từ `get_universal_system_prompt` (`system_prompt.py:378-449`), gồm:

- nội dung prompt hệ thống (`config.system_prompt` → `prompts/cli.md`, `config.py:494-503`);
- commit signature (`system_prompt.py:365-375`), tên model (`:390-391`);
- OS/shell (`system_prompt.py:343-362`);
- toàn bộ prompt riêng của tool đang active (`system_prompt.py:399-405`; mỗi tool tự nạp `.md` cùng tên — `tools/base.py:116-135`);
- `<available_skills>` (`skills/manager.py:208-224`);
- user instructions file (`system_prompt.py:23-27`, `config.py:61`) hoặc `config.instructions`;
- project context: cây thư mục + git status (`system_prompt.py:417-435`, `ProjectContextProvider.get_full_context` `:300-317`);
- project doc 1 file duy nhất tại workdir theo thứ tự `AGENTS.md`, `QQCODE.md`, `.qqcode.md` (`config.py:63`, `system_prompt.py:30-37`);
- additional directory contexts (`system_prompt.py:438-447`).

Cây thư mục: duyệt tối đa `max_depth=3`, `max_files=1000`, `max_dirs_per_level=20`, `max_chars=40_000`, timeout 2s (`config.py:122-131`), bỏ qua gitignore + default patterns (`system_prompt.py:50-99`). Git status: branch, main branch, số changes, 5 commit gần nhất (`system_prompt.py:201-298`).

### 2.2 Middleware & ngưỡng compact

`agent.py:242-257` cài middleware theo thứ tự:

```python
if max_turns is not None: ... TurnLimitMiddleware
if max_price is not None: ... PriceLimitMiddleware
context_limit = self.config.get_context_limit_for_active_model()
if context_limit > 0:
    self.middleware_pipeline.add(AutoCompactMiddleware(context_limit))
    if self.config.context_warnings:
        self.middleware_pipeline.add(ContextWarningMiddleware(0.5, context_limit))
```

- `AutoCompactMiddleware` (`middleware.py:87-107`): `context_tokens >= threshold` → `COMPACT`. Threshold chính là `context_limit` của model (`config.py:513-521`); `auto_compact_threshold` (default 200_000, `config.py:435`) chỉ dùng fallback khi alias model không tồn tại.
- `ContextWarningMiddleware` (`middleware.py:109-141`): cảnh báo 50%, **mặc định tắt** vì `context_warnings: bool = False` (`config.py:436`).
- Vòng lặp gọi middleware `before_turn` trước mỗi LLM turn (`agent.py:319-329`), `after_turn` sau đó (`agent.py:357-366`).

`stats.context_tokens` chỉ được cập nhật sau mỗi request: `prompt_tokens + completion_tokens` của response (`agent.py:790-792`, `858-860`). Nghĩa là: request thứ N có thể đã vượt ngưỡng, nhưng compact chỉ kích hoạt **trước request N+1**.

### 2.3 Thuật toán compact

`agent.py:1071-1126`:

1. `_clean_message_history()` — vá tool response thiếu (`_fill_missing_tool_responses` `agent.py:992-1032`) và đảm bảo assistant sau tool (`_ensure_assistant_after_tools` `:1034-1042`).
2. Lấy user message cuối (`:1078-1082`).
3. Append `UtilityPrompt.COMPACT.read()` (`compact.md`, 48 dòng) làm user message (`:1084-1086`).
4. Gọi `_chat()` (không stream) lấy summary (`:1088-1093`).
5. Nối thêm `"\n\nLast request from user was: {last_user_message}"` (`:1095-1098`).
6. **Thay toàn bộ history bằng** `[system_message, summary_message]` (`:1100-1102`).
7. Gọi `backend.count_tokens(...)` — **một request API thật** — để cập nhật `stats.context_tokens` (`:1107-1117`).
8. `_reset_session()` → session_id mới, file session log mới (`:1119`, `:1044-1046`), `middleware_pipeline.reset(COMPACT)` (`:1124`).

Prompt `compact.md` (`vibe/core/prompts/compact.md:1-48`) yêu cầu 7 mục: User's Primary Goals, Conversation Timeline, Technical Context, Files and Code Changes, Active Work and Last Actions, Unresolved Issues, Immediate Next Step. Chất lượng prompt tốt, nhưng **một phát duy nhất** — không có cập nhật dần, không có luật preserve chính xác file/function/error (chỉ câu "Be precise..." ở cuối).

### 2.4 Tool output limits

| Tool | Giới hạn | Bằng chứng |
|---|---|---|
| `read_file` | 64KB tổng, cắt theo dòng | `read_file.py:51-53`, `115-147` |
| `bash` | 16KB stdout + 16KB stderr, cắt `[:max_bytes]` (mất **đầu** output) | `bash.py:133-135`, `347-357` |
| `grep` | 100 match, 64KB | `grep.py:32-37`, `255-266` |
| `edit` | file ≤100KB | `edit.py:32`, `146-163` |

Không có thông báo "output đã bị cắt tại dòng X" cho model; `was_truncated` là field của result model nhưng được serialize vào tool result text dạng `k: v` (`agent.py:641-651`).

### 2.5 Session log như "bộ nhớ phụ"

Mỗi lần lưu, `InteractionLogger.save_interaction` ghi lại **toàn bộ** messages + metadata + stats + tools + agent_config vào 1 file JSON (`interaction_logger.py:155-204`), ghi lại toàn bộ file mỗi event (overwrite). Resume đọc file bằng `load_session` (`interaction_logger.py:348-356`); `--continue` lấy file mtime mới nhất (`:235-247`), `--resume` khớp id (`:249-275`). Không có append, không có entry type, không tree.

### 2.6 Hệ quả

- **Mất chi tiết sau compact**: chỉ còn summary; các tool result gần nhất bị mất dù model đang cần chúng.
- **Chi phí**: mỗi compact tốn 2 request (summary + count_tokens) trên toàn bộ history.
- **Ngưỡng trễ**: context có thể vượt ngưỡng trước khi compact chạy.
- **Provider overflow** (lỗi "prompt too long") không được phát hiện ⇒ session chết thay vì compact + retry.
- **Không thể branch**: mọi lần compact đều reset session id (`agent.py:1119`), file cũ vẫn còn nhưng không có API quay lại.

---

## 3. Pi có gì

### 3.1 Ba tầng context

| Tầng | Vai trò | Bằng chứng |
|---|---|---|
| `packages/agent` | Vòng lặp tool thuần; **không** compaction; chỉ hook `transformContext`/`convertToLlm` | `pi/packages/agent/src/types.ts:196-244` |
| `packages/coding-agent` | Compaction production cho CLI | `src/core/compaction/compaction.ts`; `docs/compaction.md` |
| `packages/durable` | Context engine theo entry + task graph, crash recovery, subagent | `src/harness/context.ts`, `compaction.ts`, `generation.ts` |

### 3.2 Ngưỡng & cơ chế compact (coding-agent)

- Defaults: `enabled: true`, `reserveTokens: 16384`, `keepRecentTokens: 20000` (`compaction/compaction.ts:120-130`).
- Trigger: `contextTokens > contextWindow - reserveTokens` (`compaction.ts:267-270`), kiểm tra trước assistant response (`agent-session.ts:766-785`), sau tool (`:2945-3085`), và bởi virtual-model prepare (`:838-841`).
- **Overflow recovery**: lỗi context-overflow hoặc `length` recoverable → compact rồi retry một lần (`agent-session.ts:3001-3045`); summarization hit token cap bị coi là fail và không persist (`compaction.ts:585-593`).
- **Cut point**: đi ngược từ tail, dồn token tới `keepRecentTokens`, cắt tại ranh giới user/assistant/bash, không tách assistant call khỏi tool result; xử lý split-turn (`compaction.ts:421-501`, `802-870`).
- **Iterative update**: nếu đã có summary trước đó, dùng `UPDATE_SUMMARIZATION_PROMPT` preserve thông tin cũ (`compaction.ts:540-579`).
- **Branch summarization** khi rời nhánh `/tree` (`branch-summarization.ts:258-364`).
- Request tóm tắt: `maxTokens = min(0.8×reserveTokens, model.maxTokens)`, tắt cache writes (`compaction.ts:619-639`, `696-766`), retry qua `retryAssistantCall`.
- Kết quả lưu entry `compaction` với `firstKeptEntryId`, `tokensBefore`, `details{readFiles,modifiedFiles}` (`compaction.ts:105-114`, `session-manager.ts:1261`).
- Extension hooks: `session_before_compact`, `session_compact`, `session_compact_failed` (`extensions/types.ts:767-791`).

Prompt tóm tắt (`compaction.ts:507-538`) yêu cầu chính xác format `## Goal / ## Constraints & Preferences / ## Progress (Done/In Progress/Blocked) / ## Key Decisions / ## Next Steps / ## Critical Context`, kèm luật "Preserve exact file paths, function names, and error messages".

### 3.3 Token accounting & context %

- Estimator: `CHARS_PER_TOKEN = 3.5`, ảnh = 4800 chars (`pi/packages/ai/src/utils/estimate.ts:15-16`); `estimateMessageTokens` (`:46-69`); `estimateContextTokens` = usage assistant gần nhất + estimate phần đuôi (`:97-112`).
- `getContextUsage()` trả `{tokens, contextWindow, percent}`; footer hiển thị `% / window (auto)` với màu cảnh báo >70/>90% (`agent-session.ts:4235-4278`; `footer.ts:160-212`).

### 3.4 Context có cấu trúc (durable)

- Context derive từ entries: head marker mới nhất + entries sau nó; entry có thể `omit`/`replace`; assistant `aborted|error|deferred` bị loại; tool result thiếu được tổng hợp `"Tool result unavailable..."` (`durable/src/harness/context.ts:9`, `95-131`, `172-209`).
- Delta read: `readContextFrom` tái dùng `ContextRange` cũ, chỉ scan entries mới (`context.ts:52-81`); scheduler cache range (`scheduler.ts:1177-1189`).
- Commit atomic có sequence; JSONL/SQLite storage; crash recovery đưa task `running → pending` (`session.ts:404-444`; `scheduler.ts:235-257`).
- Compaction durable: blocking khi `> window - 16384`, background khi `> blocking - 32768` (`harness/generation.ts:306-323`); retry riêng; hook `beforeCompact` có thể từ chối/tự cung cấp summary (`harness/compaction.ts:103-228`).
- Fork conversation tại entry, `snapshotAsOf` đọc document tại thời điểm commit (`session.ts:303-348`; `harness/forks.ts:26-59`).

### 3.5 Tool output truncation

- Constants dùng chung: 2000 dòng / 50KB, `GREP_MAX_LINE_LENGTH = 500` (`coding-agent/src/core/tools/truncate.ts:11-13`).
- `read`: head-truncate + hint `[Showing lines A-B of N. Use offset=X to continue.]` (`tools/read.ts:177-203`).
- `bash`: tail-truncate, ghi full output ra temp file và in đường dẫn (`tools/bash.ts:338-356`, `output-accumulator.ts:57-119`).
- `grep`: 100 match + nhắc tăng `limit`; `find`: 1000; `ls`: 500 (`tools/grep.ts:41`, `find.ts:41`, `ls.ts:23`).
- Durable: defaults 2000 dòng/50KB, per-tool `{maxBytes,maxLines,retain:head|tail}`; bash spill temp file (`durable/src/truncate.ts:11-12`; `harness/output.ts:34-105`; `tools/bash.ts:87-137`).

---

## 4. Bảng gap chi tiết

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| Estimator token | không | 3.5 chars/token + anchor usage | `generic.py:800-827` | `estimate.ts:15-16`, `97-112` | G3 |
| Ngưỡng compact dựa reserve | threshold = full context window | `window - reserveTokens(16k)` | `middleware.py:91-99`; `config.py:513-521` | `compaction.ts:120-130`, `267-270` | G2 |
| Giữ recent messages | không | keepRecent=20k + cut hợp lệ | `agent.py:1100-1102` | `compaction.ts:421-501` | G3 |
| Background compaction | không | có | không có | `durable/src/harness/generation.ts:306-323` | G2 |
| Overflow error recovery | không | compact + retry một lần | không có | `agent-session.ts:2945-3085`, `3001-3045` | G3 |
| Iterative summary | không | preserve + merge | `compact.md` (một phát) | `compaction.ts:540-579` | G2 |
| Split-turn handling | không | có | không có | `compaction.ts:491-500`, `942-955` | G2 |
| Branch summarization | không | có (`/tree`) | không có | `branch-summarization.ts:258-364` | G2 |
| Hook trước compact | không | `beforeCompact`/`session_before_compact` | không có | `durable/src/harness/types.ts:655-673`; `extensions/types.ts:767` | G2 |
| Context edits omit/replace | không | có | không có | `durable/src/harness/context.ts:95-131` | G3 |
| Head marker/reset context | chỉ swap list | `pi.reset`, head marker | `agent.py:1100-1102` | `durable/src/entries.ts:25-29` | G2 |
| Delta context read | không | tái dùng range | không có | `durable/src/harness/context.ts:52-81` | G2 |
| Crash recovery | không | durable commit + reopen | `interaction_logger.py:155-204` (overwrite) | `durable/src/session/session.ts:404-444`; `scheduler.ts:235-257` | G3 |
| Cảnh báo context | middleware 50%, default tắt | footer + auto warning | `middleware.py:109-141`; `config.py:436` | `footer.ts:160-212` | G1 |
| Token đo khi compact | 1 API call/thật | estimate | `agent.py:1107-1117` | `estimate.ts` | G2 |
| Truncation 2 chiều + notice | byte-cut thô | line+byte + notice + temp file | `bash.py:349-357` | `truncate.ts:11-13`; `read.ts:184-203`; `bash.ts:338-356` | G2 |
| Giữ checkpoints lịch sử | file JSON cũ còn nhưng không branch được | entry + snapshot + fork | `interaction_logger.py:65-70` | `session.ts:303-348` | G3 |

---

## 5. Thiết kế đề xuất cho QQcode

1. **P0 — Token estimator + kiểm tra trước request**: thêm `vibe/core/context/tokens.py` (`estimate_message_tokens`, `estimate_context_tokens` neo vào `stats.last_turn_prompt_tokens`). Không cần chính xác tuyệt đối; mục tiêu là biết trước khi vượt ngưỡng.
2. **P0 — Compact giữ recent**: đổi `compact()` thành `select_cut()` + `summarize()`, giữ ~20k token cuối với ranh giới không tách assistant↔tool-result. Có thể tái dùng `_fill_missing_tool_responses` (`agent.py:992-1032`).
3. **P0 — Overflow recovery**: bọc `_chat`/`_chat_streaming` bằng `match` trên error string/status cho context overflow → chạy compact + retry một lần (đặt tại `_conversation_loop`, `agent.py:317-378`).
4. **P1 — Iterative summary**: giữ summary cũ (lưu trong `AgentStats` hoặc field mới) và dùng prompt update khi compact lần 2; tái sử dụng `compact.md` + thêm prompt `update_summary.md`.
5. **P1 — Truncation chuẩn hoá**: helper chung `truncate(content, max_lines, max_bytes, retain)`; bash tail + temp file; read head + hint offset; hiển thị notice cho model.
6. **P2 — Entry-based session (nền tảng cho fork/tree/context edit)**: chuyển `InteractionLogger` từ snapshot JSON sang **append JSONL có id/parentId/type** (message/compaction/model_change...). Đây là điều kiện tiên quyết cho doc 02 (fork/tree) và doc 07 (durability). Có thể giữ reader JSON cũ để migration.

> **Counterfactual**: giữ `count_tokens` API probe và compact all-or-nothing, thì mỗi lần chạm ngưỡng session mất toàn bộ tool context gần nhất — model sẽ lặp lại các bước vừa làm; và session không bao giờ tự phục hồi sau lỗi "prompt is too long" của provider.
