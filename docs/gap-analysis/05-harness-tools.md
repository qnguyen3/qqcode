# 05 — Harness & Tools

> Nguồn: **QQcode** `@ be6a96c` · **Pi** `@ 636703a0a`. Bằng chứng dạng `file:line`.

## 1. Tóm tắt điều hành

QQcode có hệ tool Pydantic-first gọn gàng: `BaseTool[ToolArgs, ToolResult, ToolConfig, ToolState]` generic 4 tham số, tự sinh JSON schema từ `run()` annotations, discovery file-based, permission 3 mức + allow/deny list + approval callback + plan mode built-in. Đây là phần **mạnh và sạch** của QQcode, hơn Pi ở vài điểm (permission built-in, plan mode built-in, todo built-in).

Khoảng cách nằm ở **tầng thực thi và biên năng lực**:

1. **Tuần tự hoá tool calls**: `for tool_call in resolved.tool_calls` (`agent.py:551`) — không có parallel execution dù model trả nhiều call.
2. **Không có timeout/retry chung cho tool**: chỉ `bash` có timeout (`bash.py:243-246`), không có timeout wrapper nào ở agent loop.
3. **Kết quả tool bị `str()` hoá**: `"\n".join(f"{k}: {v}" ...)` (`agent.py:641-651`) — mất cấu trúc, mất field cho model; Pi giữ `content/details/structuredContent`.
4. **Output truncation thô**: cắt byte đầu tiên (`bash.py:349-357`) — bash thường cần **tail**; không temp-file spill; không notice.
5. **Thiếu tool nhóm khám phá file**: không có `find`/`ls`; model phải dùng bash hoặc grep (prompt bash.md còn hướng dẫn `bash("ls -la")`).
6. **Không có hook vòng đời** (`before/after tool call`) cho extension; chỉ có permission.

### Bảng điểm trục Harness & Tools

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| H1 | Tool built-in | 9 (bash, grep, read_file, write_file, edit, todo, skill, submit_plan, exit_plan_mode) | 8 (read, bash, powershell, edit, write, grep, find, ls) + extension | G0/G1 |
| H2 | Parallel tool execution | Không | Có (Promise.all, trừ sequential mode) | G2 |
| H3 | Per-tool timeout | Chỉ bash | Bash/PowerShell timeout; durable có output limits | G1 |
| H4 | Tool result có cấu trúc | Không (string hoá) | `content/details/usage/isError/terminate` + `structuredContent` | G2 |
| H5 | Truncation chuẩn + notice + temp file | byte-cut, không notice | line+byte, head/tail, temp file, notice | G2 |
| H6 | Permission/approval built-in | Có (ASK/NEVER/ALWAYS + allow/deny) | Không built-in, chỉ extension | G0 (QQcode nhỉnh) |
| H7 | Plan mode built-in | Có | Extension example | G0 (QQcode nhỉnh) |
| H8 | Todo tracking | Có (state in-memory) | Extension | G0 (QQcode nhỉnh) |
| H9 | Hooks before/after tool | Không | `beforeToolCall`/`afterToolCall` + durable ToolHooks | G2 |
| H10 | Output schema cho tool | Không | `outputSchema`/structuredContent cho codemode | G1 |
| H11 | `find`/`ls` tools | Không | Có | G1 |
| H12 | File mutation queue | Không (chỉ edit tuần tự) | Có (`file-mutation-queue.ts`) | G2 |
| H13 | Grep/find dùng fd/ripgrep + ignore | rg/grep + exclude list | rg/fd + git-aware ignore | G1 |
| H14 | Sandbox tool script (codemode) | Không | QuickJS sandbox | G2 |
| H15 | Tool search/deferred exposure | Không | BM25 `tool_search`, exposure modes | G2 |
| H16 | Bash env hygiene | Có (`CI`, no-color, TERM=dumb) | Tương tự + detached process-group kill | G0 |
| H17 | Image read | Không | Có (jpg/png/gif/webp/bmp + resize) | G2 |

---

## 2. QQcode hiện có gì

### 2.1 Kiến trúc tool

- `BaseTool` generic 4 type param (`tools/base.py:93-283`): `description` ClassVar, `run(args) -> ToolResult`, `get_tool_prompt()` tự tìm `.md` cùng tên (`base.py:116-135`), `invoke()` validate Pydantic rồi gọi `run` (`base.py:137-149`), `get_parameters()` sinh JSON schema đã dọn `title` (`base.py:237-258`), `get_name()` snake_case từ class name (`base.py:260-264`).
- `BaseToolConfig` (`base.py:54-84`): `permission`, `workdir`, `allowlist`, `denylist`, `extra="allow"` cho config riêng.
- `ToolState` giữ state trong RAM giữa các call (todo list, recently read files); `reset_all()` clear instance (`manager.py:288-289`).
- Discovery: quét `*.py` trong `DEFAULT_TOOL_DIR` (`vibe/core/tools/builtins`), `config.tool_paths`, `.qqcode/tools` (đi ngược parent), `~/.qqcode/tools` (`manager.py:58-85`); import module động, gom class con của BaseTool (`manager.py:87-120`).
- Tool active = tất cả tool discover được, lọc bởi `enabled_tools`/`disabled_tools` (glob/regex) (`format.py:77-102`).
- JSON schema + description gửi API qua `APIToolFormatHandler.get_available_tools` (`format.py:147-161`), `tool_choice` luôn `"auto"` (`format.py:163-164`).

### 2.2 Danh sách tool built-in & hành vi

| Tool | Args | Hành vi | Bằng chứng |
|---|---|---|---|
| `bash` | `command`, `timeout?` | subprocess shell, env sạch, kill process group, allow/deny list, 16KB cap | `bash.py:242-374`, `81-150` |
| `read_file` | `path`, `offset?`, `limit?` | UTF-8, tối đa 64KB; history 10 file gần đọc | `read_file.py:28-57`, `115-147` |
| `write_file` | `path`, `content`, `overwrite?` | fail nếu tồn tại trừ khi `overwrite=true` | `write_file.py` |
| `edit` | `file_path`, `old_string`, `new_string`, `replace_all?` | exact replace, unique trừ replace_all, max file 100KB | `edit.py:14-21`, `85-123` |
| `grep` | `pattern`, `path`, `max_matches?`, `use_default_ignore?` | rg nếu có, fallback grep; exclude list; 100 match/64KB | `grep.py:79-105`, `255-266` |
| `todo` | `action read/write`, `todos?` | state in-memory, max 100, id unique; validator chữa GLM trả string | `todo.py:40-79`, `151-183` |
| `skill` | `name` | đọc full SKILL.md trả về model | `skill.py:21-98` |
| `submit_plan` | `plan` | plan mode; approval qua callback agent | `plan_mode.py:19-58`; `agent.py:616-674` |
| `exit_plan_mode` | — | thoát plan mode | `plan_mode.py:66-100` |

MCP tool được sinh proxy class runtime với tên `{alias}_{tool}` và schema thô từ server (`mcp.py:126-207`, `230-313`).

### 2.3 Luồng thực thi

`agent.py:531-751`:

1. Failed calls (unknown tool/sai args) → tool result lỗi + append message (`:534-549`).
2. Với từng tool call tuần tự (`:551`):
   - emit `ToolCallEvent`;
   - `_should_execute_tool` (`:889-952`) quyết định theo mode:
     - PLAN: allowlist `{read_file, grep, todo, submit_plan, bash, skill}` (`agent.py:69-71`), bash dùng `BashPlanModeConfig` (`bash.py:224-239`);
     - AUTO_APPROVE: luôn chạy;
     - INTERACTIVE: allow/deny list (`check_allowlist_denylist`) → config `permission` → approval callback.
   - `await tool_instance.invoke(**args)` (`:613`), đo duration;
   - nối result vào messages dạng `"\n".join(f"{k}: {v}")` (`:641-651`);
   - bắt `CancelledError`, `KeyboardInterrupt`, `ToolError`, `ToolPermissionError` (`:685-751`).
3. Không có timeout bao ngoài; không chạy song song; không retry tool.

Approval callback do UI set (`app.py:681-700`), gồm option "always" (set auto-approve — `agent.py:970-979`).

### 2.4 Điểm mạnh riêng

- **Permission built-in** với phân cấp rõ: mode > allow/deny list > config permission > user approval (`agent.py:889-952`). Pi cố tình **không** có permission built-in (README `pi/README.md:90`), chỉ có extension `permission-gate.ts`.
- **Plan mode built-in** với system prompt riêng, tool allowlist, bash read-only allow/deny (`plan_mode.md`; `bash.py:153-239`) — Pi chỉ có extension `plan-mode/`.
- **Todo built-in** với UI view riêng (`Ctrl+T`, `commands.py:93`) — Pi chỉ có extension `todo.ts`.
- **Dangerous directory detection** (`utils.py:105-141`) tắt quét project khi ở home/system dir — Pi không có (nhưng Pi có trust + containerization).

---

## 3. Pi có gì

### 3.1 Tool definitions & validation

- Tool = `name/description/parameters: TypeBox` + `executionMode`, `replay`, `prepareArguments`, `outputLimits` (`durable/src/harness/types.ts:208-236`); helper `defineTool` (`harness/define.ts:13-17`).
- Validation TypeBox: clone args, drop optional null, `Value.Convert`, coerce, compile-cache (`ai/src/utils/validation.ts:317-349`).
- `prepareArguments` sửa shape trước validation, ví dụ edit nhận `edits` dạng string/single/legacy (`durable/src/tools/edit.ts:45-71`).

### 3.2 Execution

- Một tool-call = một durable `ToolTask` với phase `call → execute`; intent `{arguments, replay}` được commit **trước khi chạy** (`durable/src/harness/tool.ts:55-91`).
- Parallel/sequential quyết định theo config hoặc `executionMode: "sequential"`; parallel tạo tất cả task rồi `allSettled` (`harness/generation.ts:545-591`).
- Kết quả hỗ trợ `content/details/usage/isError/terminate` + `structuredContent` (`agent/src/types.ts:423-446`).
- Recovery: interrupted tool chỉ rerun nếu `replay: "safe"` ở cả stored + current policy (`harness/tool.ts:93-111`).
- Output bounding per-tool + defaults, streamed window, dropped counts (`harness/output.ts:34-227`); bash spill full output temp file + diagnostic (`tools/bash.ts:87-137`).

### 3.3 Tool bộ built-in (coding-agent)

`read` (2000 dòng/50KB + ảnh + offset hint), `bash` (tail truncate + temp file), `powershell`, `edit` (multi-edit trên original, diff/patch trả về, mutation queue), `write` (mkdir recursive, mutation queue), `grep` (rg JSON, 100 match, 500 char/line), `find` (fd, git-aware), `ls` (500 entries) (`tools/index.ts:93-105`; hành vi từng tool trong `src/core/tools/*.ts`).

Tool snippets/guidelines nằm cạnh tool và chảy vào system prompt (`system-prompt.ts:118-125`, `157-161`).

### 3.4 Hook vòng đời tool

- Agent core: `beforeToolCall` (block/terminate), `afterToolCall` (override từng field), expose cho tool gọi tool (`agent/src/types.ts:319-341`; `agent-loop.ts:727-903`).
- Durable: `beforeTool` (block/replace args), `afterTool` (replace result) (`durable/src/harness/types.ts:638-653`).
- Extension: event `tool_call`/`tool_result` + `ctx.executeTool` nested (`extensions/types.ts:1157`, `1231`).

### 3.5 Extension tool & codemode

- `registerTool` cho extension (`extensions/types.ts:1634`), exposure `direct/model-only/codemode/deferred/hidden`.
- `codemode`: script JS chạy QuickJS WASI gọi tool khác, classifier, image models (`extensions/codemode/tool.ts:89-298`; `docs/codemode.md`).
- `tool_search`: BM25 trên metadata tool deferred, expose match cho model call kế tiếp (`extensions/tool-search/tool.ts:159-224`).

---

## 4. Bảng gap chi tiết

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| Parallel tool calls | không | có | `agent.py:551` | `agent/src/agent-loop.ts:508-660`; `harness/generation.ts:545-591` | G2 |
| Timeout chung cho tool | không | một phần (bash/pwsh) | `agent.py:613` | `durable/src/tools/bash.ts:10-18` | G1 |
| Result cấu trúc | string hoá `k: v` | detail + structuredContent | `agent.py:641-651` | `agent/src/types.ts:423-446` | G2 |
| Truncation thông minh + notice | byte-cut đầu | line/byte, head/tail, temp file | `bash.py:349-357` | `truncate.ts:11-13`; `bash.ts:338-356` | G2 |
| Hook tool_call/tool_result | không | có (agent + durable + extension) | không có | `agent/src/types.ts:319-341`; `extensions/types.ts:1157` | G2 |
| `find`/`ls` tools | không | có | tool list không có | `tools/index.ts:93-105` | G1 |
| File mutation queue | không | có | `edit.py:84-123` | `tools/file-mutation-queue.ts` | G2 |
| Read ảnh | không | có | `read_file.py:121` text-only | `tools/read.ts:96`, `129-152` | G2 |
| Output schema/`structuredContent` | không (MCP có) | có | `mcp.py:69-99` | `agent/src/types.ts:429-433` | G1 |
| Codemode sandbox | không | có | không có | `extensions/codemode/tool.ts:89` | G2 |
| Deferred tool exposure/search | không | có | không có | `extensions/tool-search/tool.ts:159-224` | G2 |
| Recovery tool sau crash | không | durable replay policy | không có | `durable/src/harness/tool.ts:93-111` | G2 |
| Permission built-in | có | không (extension) | `agent.py:889-952` | `README.md:90` | G0 |
| Plan mode built-in | có | extension | `agent.py:69-71`; `plan_mode.md` | `examples/extensions/plan-mode` | G0 |
| Todo built-in | có | extension | `todo.py` | `examples/extensions/todo.ts` | G0 |
| Bash plan-mode allow/deny | có, chi tiết | không built-in | `bash.py:153-239` | — | G0 |

---

## 5. Ưu tiên

1. **P0 — Parallel execution + timeout wrapper**: chạy các tool call độc lập bằng `asyncio.gather`; thêm `BaseToolConfig.timeout` mặc định cho mọi tool (bash giữ override riêng). Lưu ý giữ đúng thứ tự message tool result (`agent.py:645-651`).
2. **P0 — Tool result có cấu trúc + truncation chuẩn**: thêm helper `truncate(content, max_lines, max_bytes, retain)` vào `vibe/core/tools/` và dùng chung; bash chuyển sang tail + temp file; thêm marker `[output truncated ...]` vào result text.
3. **P1 — Hook vòng đời tool**: mở rộng `BaseTool`/Agent bằng `before_tool_call`/`after_tool_call` callback trong config hoặc registry để custom tool/extension can thiệp (điều kiện cho doc 06).
4. **P1 — Thêm `find`/`ls` tool**: dễ, tăng độ chính xác khám phá file, giảm phụ thuộc bash; cập nhật `bash.md` prompt tương ứng.
5. **P2 — File mutation queue**: tuần tự hoá ghi cùng path để tránh race khi parallel tools bật.
6. **P2 — `tool_search`/deferred exposure**: khi số MCP tool lớn (serena_*...), thêm cơ chế ẩn/hiện tool theo nhu cầu như Pi.

> **Counterfactual**: nếu không có timeout chung, một tool MCP treo sẽ treo cả agent loop (chỉ bash tự cứu được). Nếu không cấu trúc hoá result, model khó phân biệt `was_truncated=true` với nội dung thật, dẫn tới đọc thiếu dữ liệu mà không biết.
