# Bộ tài liệu Gap: QQcode vs Pi

Tài liệu phân tích khoảng cách giữa **QQcode** và **Pi** theo 7 trục: Providers/Auth, Commands/CLI, Context Engineering, Prompt Engineering, Harness/Tools, Extensibility, Sessions/Security/Ops — kèm roadmap.

## Nguồn & phạm vi

| | QQcode | Pi |
|---|---|---|
| Repo | `/home/toan/build/qqcode` | `/home/toan/build/pi` |
| Commit | `be6a96c` (v1.2.0) | `636703a0a` (`main`) |
| Ngôn ngữ | Python 3.12, package `vibe` | TypeScript (Node/Bun), monorepo 16 package |
| Quy mô | 111 file `.py` / ~19.9k LOC trong `vibe/` | 1.566 file `.ts` / ~380k LOC trong `packages/` |
| Nguồn gốc | fork của Mistral Vibe (`README.md:22-24`) | harness độc lập `@earendil-works/pi-coding-agent` |
| Docs | không có thư mục docs | 40 tài liệu `.md` trong `packages/coding-agent/docs/` (+ `docs.json`) |

**Phương pháp**: đọc tuần tự từ manifest → entrypoint → core loop → tool/LLM layer → session/extensibility; mọi khẳng định đều truy vết `file:line` trong code. Không suy đoán tính năng không thấy trong code.

**Quy ước mức độ gap**:

| Ký hiệu | Nghĩa |
|---|---|
| **G3** | Thiếu nghiêm trọng — ảnh hưởng trực tiếp chất lượng agent hoặc chặn năng lực cốt lõi |
| **G2** | Thiếu đáng kể — có giải pháp thay thế yếu hơn hoặc phải sửa core |
| **G1** | Thiếu nhỏ — tiện ích/parity, chi phí thấp |
| **G0** | Tương đương, hoặc QQcode nhỉnh hơn, hoặc khác hướng có chủ đích |

**Bằng chứng**: `qqcode/vibe/...:line` và `pi/packages/...:line`. Số dòng lấy từ checkout tại thời điểm phân tích; nếu rebase, số dòng có thể lệch nhưng tên file/hàm giữ nguyên.

## Snapshot kiến trúc

### QQcode (3 tầng)

```
vibe/cli/entrypoint.py           # argparse → interactive | programmatic | ACP
  └─ textual_ui/app.py           # Textual TUI, slash commands, approval UI
vibe/core/agent.py               # Agent: vòng lặp conversation + tool calls
  ├─ middleware.py               # AutoCompact, ContextWarning, Turn/Price limit
  ├─ llm/backend/{mistral,generic,anthropic_sdk}.py
  ├─ tools/{base,manager,mcp}.py + builtins/{bash,grep,read_file,write_file,edit,todo,skill,plan_mode}
  ├─ skills/manager.py           # .qqcode/skills + ~/.qqcode/skills
  ├─ system_prompt.py            # prompt concat + project context + git status
  └─ interaction_logger.py       # JSON snapshot session
vibe/acp/                        # ACP server cho client ngoài (Zed...)
vscode-extension/                # UI VSCode, gọi qua --output vscode / --list-sessions
```

### Pi (nhiều tầng)

```
packages/coding-agent/           # CLI: interactive | print | json | rpc + SDK
  ├─ src/core/system-prompt.ts   # prompt dạng section, diff để giữ cache
  ├─ src/core/compaction/        # compact + iterative + branch summary
  ├─ src/core/tools/             # read/bash/edit/write/grep/find/ls
  ├─ src/core/extensions/        # ~40 event hooks + register*
  ├─ src/core/skills.ts          # Agent Skills spec, progressive disclosure
  ├─ src/core/session-manager.ts # JSONL tree, fork/clone, export/import
  └─ src/core/package-manager.ts # pi install npm/git/local
packages/agent/                  # agent loop thuần, tool calling, steering/followUp
packages/ai/                     # 42 provider, 10 API, OAuth, model catalog, caching
packages/durable/                # task graph, context engine, crash recovery, subagent
packages/chord/                  # replicated state, services, delta, plugin runtime
packages/telemetry/ packages/evals/ packages/mcp/ packages/tui/ ...
```

### Flow runtime để đối chiếu

**QQcode `qqcode -p "fix bug" --auto-approve`**:
`entrypoint.main()` (`cli/entrypoint.py:178`) → `run_programmatic()` (`core/programmatic.py:144`) → `Agent.act()` (`core/agent.py:237`) → `_conversation_loop` (`:313`) → middleware `run_before_turn` (compact nếu cần) → `_chat_streaming` (`:809`) → backend (`llm/backend/*`) → parse tool calls (`llm/format.py:194`) → `_handle_tool_calls` tuần tự (`:531`) → lưu session JSON (`interaction_logger.py:155`) → quay lại đầu loop.

**Pi `pi -p "fix bug"`**:
`main()` (`coding-agent/src/main.ts`) → mode print (`modes/print-mode.ts`) → `AgentSession.prompt()` (`core/agent-session.ts`) → agent loop (`packages/agent/src/agent-loop.ts`) → trước request: compaction check (`core/compaction/compaction.ts:267`) → provider stream (`packages/ai`) → tool round (parallel/sequential, ghi durable Task) → session append entry (`session-manager.ts`) → in kết quả/text hoặc JSONL event.

## Bảng tổng hợp gap (106 hạng mục)

| Trục | G3 | G2 | G1 | G0 | Tài liệu | Gap nghiêm trọng nhất |
|---|---|---|---|---|---|---|
| Providers & Auth | 2 | 8 | 3 | 1 | `01-providers-auth.md` | Model catalog tĩnh, không capability metadata |
| Commands & CLI | 2 | 6 | 6 | 1 | `02-commands-cli.md` | Không RPC; không fork/tree session |
| Context Engineering | 5 | 7 | 3 | 0 | `03-context-engineering.md` | Không estimator; compact all-or-nothing; không overflow recovery |
| Prompt Engineering | 0 | 5 | 4 | 5 | `04-prompt-engineering.md` | Prompt rewrite toàn bộ khi đổi mode → phá cache |
| Harness & Tools | 0 | 8 | 5 | 4 | `05-harness-tools.md` | Tool tuần tự; result bị string hoá; truncation thô |
| Extensibility | 2 | 8 | 4 | 2 | `06-extensibility.md` | Không hook/event API; không package ecosystem |
| Sessions/Security/Ops | 4 | 7 | 3 | 1 | `07-sessions-security-ops.md` | Session snapshot ghi đè; không crash recovery/subagent |
| **Tổng** | **15** | **49** | **28** | **14** | | |

## Top 10 gap xuyên trục (theo ROI)

| # | Gap | Trục | Bằng chứng then chốt | Ảnh hưởng |
|---|---|---|---|---|
| 1 | Đo token bằng API call + compact all-or-nothing | Context | `qqcode/vibe/core/agent.py:1071-1126`, `qqcode/vibe/core/llm/backend/generic.py:800-827` vs `pi/packages/ai/src/utils/estimate.ts:97-112`, `pi/packages/coding-agent/src/core/compaction/compaction.ts:421-501` | Mất chi tiết gần nhất mỗi lần compact; tốn request |
| 2 | Không phát hiện overflow để compact + retry | Context | không có (QQcode) vs `pi/packages/coding-agent/src/core/agent-session.ts:3001-3045` | Session chết ở "prompt too long" |
| 3 | Session JSON snapshot ghi đè, không tree/fork | Sessions | `qqcode/vibe/core/interaction_logger.py:155-204` vs `pi/packages/coding-agent/docs/session-format.md:62-160` | Mất hội thoại khi crash; không thử hướng khác |
| 4 | Model catalog tĩnh, không capabilities | Providers | `qqcode/vibe/core/config.py:313-427` vs `pi/packages/ai/scripts/generate-models.ts` | Thêm model/provider = sửa code + release |
| 5 | Prompt bị thay toàn bộ khi đổi mode | Prompt | `qqcode/vibe/core/agent.py:1134-1146` vs `pi/packages/coding-agent/src/core/system-prompt.ts:217-229` | Cache miss đúng luồng plan mode |
| 6 | Tool chạy tuần tự, không timeout chung | Harness | `qqcode/vibe/core/agent.py:551` vs `pi/packages/agent/src/agent-loop.ts:508-660` | Chậm; một tool treo khoá cả loop |
| 7 | Không hook/event API cho extension | Extensibility | không có vs `pi/packages/coding-agent/src/core/extensions/types.ts:682-1231` | Mọi tính năng mới phải fork core |
| 8 | Tool result bị string hoá `k: v` | Harness | `qqcode/vibe/core/agent.py:641-651` vs `pi/packages/agent/src/types.ts:423-446` | Model khó phân biệt field/truncation |
| 9 | Không có output/context % và cost/cache entry | Ops | `qqcode/vibe/core/types.py:74-89` vs `pi/packages/coding-agent/src/modes/interactive/components/footer.ts:179-212` | Người dùng không biết chi phí/ngưỡng |
| 10 | Không prompt templates / commands động | Commands | không có vs `pi/packages/coding-agent/src/core/prompt-templates.ts:25-103` | Workflow lặp lại phải gõ tay |

## Điểm QQcode nhỉnh hơn Pi (giữ và khai thác)

| Hạng mục | QQcode | Pi |
|---|---|---|
| Permission/approval built-in (3 mức + allow/deny + UI) | `qqcode/vibe/core/tools/base.py:39-69`; `agent.py:889-952` | không built-in (`pi/README.md:90`) |
| Plan mode built-in (prompt + tool allowlist + bash read-only) | `qqcode/vibe/core/prompts/plan_mode.md`; `bash.py:153-239` | extension example |
| Todo built-in + view | `qqcode/vibe/core/tools/builtins/todo.py`; UI `Ctrl+T` | extension example |
| Cảnh báo thư mục nguy hiểm | `qqcode/vibe/core/utils.py:105-141` | không có |
| ACP server chuẩn hoá | `qqcode/vibe/acp/` | RPC/SDK thay thế |

## Danh mục tài liệu

| File | Nội dung |
|---|---|
| `01-providers-auth.md` | 42 provider vs 3 backend; OAuth; model catalog; caching; reasoning; cost; retry |
| `02-commands-cli.md` | CLI flags; slash commands; RPC/JSON/SDK; session & sharing UX |
| `03-context-engineering.md` | Compact; token estimate; recent window; overflow; context edits; durability |
| `04-prompt-engineering.md` | System prompt sections; context files; prompt templates; skills prompt; cache |
| `05-harness-tools.md` | Tool loop; parallel/timeout; result structure; truncation; hooks; tool list |
| `06-extensibility.md` | Skills; MCP; extensions; themes; packages; custom providers |
| `07-sessions-security-ops.md` | Session format/tree; durability; permission/trust/sandbox; telemetry/evals |
| `08-roadmap.md` | Thứ tự ưu tiên, phase, dependency graph, chỉ số theo dõi |

## UNKNOWN / giới hạn của phân tích

- Chưa benchmark thực tế hành vi agent (số turn trung bình, tỷ lệ hoàn thành task) cho cả hai — mọi kết luận là từ code, không từ đo lường runtime.
- Các package Pi ít liên quan tới coding-agent (`codemode`, `chord` plugin runtime, `env` daemon, `protocol/server/client`) chỉ được khảo sát ở mức kiến trúc, chưa audit từng file.
- `snapshot_report.html` (313KB) trong QQcode chưa được phân tích — có thể chứa dữ liệu UI snapshot nhưng không phải nguồn logic.
- Chi tiết OAuth bên thứ ba (scope/endpoint có thể thay đổi theo provider) không được xác minh với tài liệu provider.
- Pi có thư mục `packages/ai/src/providers/data/*.json` không hiện diện trong checkout (sinh lúc build), nên các con số model cụ thể chưa kiểm chứng từng dòng.
