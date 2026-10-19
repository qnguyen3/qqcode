# 02 — Commands, CLI modes & UX surface

> Nguồn: **QQcode** `@ be6a96c` · **Pi** `@ 636703a0a`. Bằng chứng dạng `file:line`.

## 1. Tóm tắt điều hành

QQcode có một CLI `argparse` 17 flag, 10 slash command, 4 output format, và một ACP server. Pi có 4 chế độ chạy (interactive/print/json/rpc) + SDK, **24 slash command built-in** + command từ extension/prompt-template/skill, ~40 flag, keybinding tuỳ biến, theme, session tree, export/share. Khoảng cách chủ yếu ở **bề mặt điều khiển phiên** (session/tree/export/share/model scoping) và **khả năng lập trình hoá** (RPC/SDK/prompt templates).

### Bảng điểm trục Commands & CLI

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| C1 | CLI flag | 17 | ~40 + extension flags | G1 |
| C2 | Slash commands built-in | 10 | 24 (+ `/mcp`, `/llama`, `/skill:*`, template, extension) | G2 |
| C3 | Output modes | text/json/streaming/vscode | text/print + JSONL + RPC | G1 |
| C4 | RPC server | Không | 33 lệnh JSONL | G3 |
| C5 | SDK lập trình | `run_programmatic()` nội bộ | `@earendil-works/pi-coding-agent` SDK public | G2 |
| C6 | Prompt templates | Không | `$1/$@/${...}` + `/tên-file` | G2 |
| C7 | Session tree/fork/clone | Không | `/tree`, `/fork`, `/clone`, `--fork` | G3 |
| C8 | Export / import / share | Không (chỉ JSON session nội bộ) | HTML/JSONL export, `/import`, `/share` gist | G2 |
| C9 | Model scoping/cycling | Config app đổi active model | `/model`, `/scoped-models`, Ctrl+P, `:thinking` suffix | G1 |
| C10 | Keybindings tuỳ biến | Không | `keybindings.json` + `/hotkeys` | G2 |
| C11 | Themes | Textual built-in themes | theme JSON + light/dark + hot reload | G1 |
| C12 | TUI mode | 1 màn hình | regular/fullscreen | G1 |
| C13 | Custom system prompt CLI | Không (chỉ file/agent TOML) | `--system-prompt`, `--append-system-prompt` | G2 |
| C14 | Session naming | Không | `--name`, `/name` | G1 |
| C15 | ACP | Có (agent-client-protocol 0.6.3) | Không (thay bằng RPC/SDK/plugin) | G0 (khác hướng) |

---

## 2. QQcode hiện có gì

### 2.1 CLI flags

`qqcode/vibe/cli/entrypoint.py:27-146` — danh sách đầy đủ:

| Flag | Ý nghĩa | Dòng |
|---|---|---|
| `initial_prompt` (positional) | prompt mở đầu interactive | `29-34` |
| `-p/--prompt [TEXT]` | programmatic mode | `35-43` |
| `--auto-approve` | bỏ qua approval | `44-49` |
| `--mode plan\|interactive\|auto-approve` | chế độ programmatic | `50-58` |
| `--max-turns N` | giới hạn turn (programmatic) | `59-65` |
| `--max-price $` | giới hạn chi phí | `66-72` |
| `--enabled-tools TOOL` (lặp) | whitelist tool + pattern | `73-81` |
| `--output text\|json\|streaming\|vscode` | format output | `82-91` |
| `--agent NAME` | nạp `~/.qqcode/agents/NAME.toml` | `92-97` |
| `--setup` | onboarding API key | `98` |
| `--login anthropic\|qwen` | OAuth login | `99-104` |
| `--list-sessions` / `--get-session` | liệt kê/đọc session JSON | `105-115` |
| `--list-models` / `--get-model` | liệt kê/đọc model JSON | `116-125` |
| `--model ALIAS` | override model tạm thời | `126-131` |
| `-c/--continue` | session gần nhất | `133-140` |
| `--resume SESSION_ID` | resume theo id (partial match) | `141-145` |

Ngoài ra có env override `QQCODE_MODEL_OVERRIDE` (`entrypoint.py:222-231`, `319-328`).

### 2.2 Slash commands

`qqcode/vibe/cli/commands.py:18-70` — 10 mục, nhiều alias:

| Command | Alias | Handler | Dòng |
|---|---|---|---|
| `help` | `/help`, `/h` | `_show_help` | `19-23` |
| `status` | `/status`, `/stats` | `_show_status` | `24-28` |
| `config` | `/config`, `/cfg`, `/theme`, `/model` | `_show_config` | `29-33` |
| `reload` | `/reload`, `/r` | `_reload_config` | `34-38` |
| `clear` | `/clear`, `/reset` | `_clear_history` | `39-43` |
| `log` | `/log`, `/logpath` | `_show_log_path` | `44-48` |
| `conversations` | `/conversations`, `/sessions`, `/history` | `_show_conversations` | `49-53` |
| `compact` | `/compact`, `/summarize` | `_compact_history` | `54-58` |
| `exit` | `/exit`, `/quit`, `/q` | `_exit_app` | `59-64` |
| `add_dir` | `/add-dir`, `/add-directory` | `_add_directory_context` | `65-69` |

Handler nằm trong `textual_ui/app.py` (`_show_help:806`, `_show_status:810`, `_show_config:832`, `_reload_config:838`, `_clear_history:867`, `_show_log_path:905`, `_show_conversations:938`, `_compact_history:954`, `_add_directory_context:1008`) và dispatch ở `app.py:503` (`_handle_command`).

Đổi model/provider không có slash command riêng — `/model` là alias của `/config`, mở `ConfigApp` cycle provider/model (`config_app.py:56-82`, `162-163`).

Ngoài slash command: `!<command>` chạy bash trực tiếp (`app.py:526-567`), `@path` autocompletion (`app.py:_handle_user_message`, `core/autocompletion/`), `Shift+Tab` đổi mode, `Ctrl+O` xem tool output, `Ctrl+T` todo (`commands.py:85-103`).

### 2.3 Output modes & ACP

- `OutputFormat` 4 giá trị (`types.py:281-286`); formatter tương ứng (`output_formatters.py:45-244`): `TextOutputFormatter`, `JsonOutputFormatter` (in toàn bộ messages ở cuối), `StreamingJsonOutputFormatter` (JSONL mỗi message), `VScodeJsonFormatter` (event `thread.started/item.updated/tool.call/tool.result/turn.completed/error`, `output_formatters.py:85-227`).
- `run_programmatic()` là entry nội bộ cho `-p` (`programmatic.py:144-219`), approval qua stdin JSON (`programmatic.py:37-141`).
- ACP server `qqcode-acp` (`pyproject.toml:69`), implement `Agent` từ `agent-client-protocol` (`acp/acp_agent.py:81-543`), hỗ trợ session mới/load, set model/mode, permission options (`acp/utils.py:77-119`), tool update notifications.
- Session listing cho UI/VSCode qua JSON stdout (`entrypoint.py:233-277`); VSCode extension gọi qua các flag này (`vscode-extension/src/qqcodeBackend.ts`).

### 2.4 Thiếu gì (đối chiếu nhanh)

Không có: RPC server, session tree/fork/clone, export HTML, import session, share, prompt templates, keybindings config, `/trust`, `/bug`, `/copy`, `/new`, `/resume` (chỉ có flag), `/name`, `/changelog`, `/hotkeys`, `/thinking`, `/scoped-models`, model cycling hotkey, `--system-prompt`/`--append-system-prompt`, `--no-context-files`, `--no-session`, `--tools`/`--exclude-tools` (chỉ có `--enabled-tools` một chiều), `--print` một phát (tương đương `-p` nhưng không có chế độ `--mode json` event stream như Pi), `--fork`, `--session-dir`, `--list-models` có nhưng không search.

---

## 3. Pi có gì

### 3.1 Bốn chế độ

- Resolve mode: explicit `--mode rpc|json` > `--print`/non-TTY ⇒ print > interactive (`pi/packages/coding-agent/src/main.ts:112-123`).
- **Interactive**: TUI đầy đủ.
- **Print**: text hoặc JSONL event stream (`modes/print-mode.ts:108-156`); JSON schema event tài liệu hoá (`docs/json.md`, `docs/message-types.md`).
- **RPC**: dispatch 33 lệnh (`modes/rpc/rpc-mode.ts:394-680`), union lệnh `modes/rpc/rpc-types.ts:22-74`: `prompt`, `steer`, `follow_up`, `abort`, `clear_queue`, `new_session`, `get_state`, `set_model`, `cycle_model`, `get_available_models`, `set_thinking_level`, `cycle_thinking_level`, `get_available_thinking_levels`, `set_steering_mode`, `set_follow_up_mode`, `compact`, `set_auto_compaction`, `set_auto_retry`, `abort_retry`, `bash`, `abort_bash`, `get_session_stats`, `export_html`, `switch_session`, `fork`, `clone`, `get_fork_messages`, `get_entries`, `get_tree`, `get_last_assistant_text`, `set_session_name`, `get_messages`, `get_commands`.
- **SDK**: package public + docs (`docs/sdk.md`, `examples/sdk/01..14`).

### 3.2 Slash commands (24 built-in)

`pi/packages/coding-agent/src/core/slash-commands.ts:19-43`:

`/settings`, `/model`, `/tree`, `/thinking`, `/scoped-models`, `/export`, `/import`, `/share`, `/bug`, `/copy`, `/name`, `/session`, `/changelog`, `/hotkeys`, `/fork`, `/clone`, `/trust`, `/login`, `/logout`, `/new`, `/compact`, `/resume`, `/reload`, `/quit`; thêm `/debug` ẩn + easter egg (`interactive-mode.ts:3300-3313`), `!cmd`/`!!cmd` (`:3327-3342`).

Command động: extension `registerCommand` (`extensions/types.ts:1643`), prompt template theo tên file (`prompt-templates.ts:129`), skill `/skill:<name>` (`agent-session.ts:2145-2169`), built-in `/mcp`, `/llama`.

### 3.3 Flags đáng chú ý

Parser `cli/args.ts:72-271`, help `:300-346`:

- `--mode text|json|rpc`, `--print/-p`, `--continue/-c`, `--resume/-r`, `--session`, `--session-id`, `--fork`, `--session-dir`, `--no-session`, `--name`.
- `--system-prompt <text|file>`, `--append-system-prompt` (lặp) (`args.ts:121-125`).
- `--models` (scope Ctrl+P, glob + `:thinking`), `--tools/-t`, `--exclude-tools/-xt`, `--no-tools`, `--no-builtin-tools`.
- `--thinking off|minimal|...|max`, `--export`, `--extension/-e`, `--no-extensions`, `--no-mcp`, `--skill`, `--no-skills`, `--prompt-template`, `--theme`, `--use-theme`, `--no-context-files`, `--list-models`, `--tui-mode regular|fullscreen`, `--approve/--no-approve`, `--offline`, `@file` attachment, extension flags.
- `pi auth ...` command (print-api-key/print-bearer-token/check) (`cli/auth-command.ts:19-45`), `pi mcp ...` (`extensions/mcp/cli.ts:33-73`), `pi install/remove/update/list/config`.

### 3.4 Session & sharing commands

`/tree` (branch navigation + branch summarization — `interactive-mode.ts:3263`), `/fork` (`:3253`), `/clone` (`:5517`), `/export` HTML/JSONL (`:6493`), `/import` (`:6540`), `/share` lên Radius artifact hoặc GitHub gist (`session-share.ts:56-210`), `/resume` picker, `/name`.

---

## 4. Bảng gap chi tiết

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| RPC điều khiển từ ngoài | Không | 33 lệnh qua JSONL | không có | `rpc-types.ts:22-74`; `rpc-mode.ts:394-680` | G3 |
| JSON event stream khi chạy | `--output vscode` (event tự định nghĩa) | `--mode json` (event schema ổn định + docs) | `output_formatters.py:85-227` | `modes/print-mode.ts:108-156`; `docs/json.md` | G1 |
| Fork/tree/clone | Không | Có | `interaction_logger.py` (chỉ list/resume) | `interactive-mode.ts:3253-3262`, `5517` | G3 |
| Export HTML / import | Không | Có | không có | `interactive-mode.ts:6493-6582` | G2 |
| Chia sẻ session | Không | Radius artifact/gist | không có | `session-share.ts:56-210` | G2 |
| Prompt template | Không | `$1/$@/${N:-default}` | không có | `prompt-templates.ts:25-103` | G2 |
| Keybindings tuỳ biến | Không | JSON + `/hotkeys` | không có | `docs/keybindings.md:7-28`; `interactive-mode.ts:3248` | G2 |
| Theme tuỳ biến | Chỉ chọn Textual theme | Theme JSON + hot reload | `config_app.py:15`, `THEMES` | `docs/themes.md`; `theme-controller.ts` | G1 |
| Đổi model nhanh | Chỉ qua `/config` | `/model`, Ctrl+P, `:thinking` suffix | `config_app.py:56-82` | `interactive-mode.ts:3195`, `4517`; `args.ts:117-118` | G1 |
| Scope model cycle | Không | `/scoped-models`, `--models` | không có | `interactive-mode.ts:3190`, `5357` | G2 |
| System prompt override runtime | Không | `--system-prompt`, `--append-system-prompt` | không có (chỉ config/agent file) | `args.ts:121-125` | G2 |
| Tắt context files | Không | `--no-context-files` | không có | `args.ts:216-217` | G1 |
| Đặt tên session | Không | `--name`, `/name` | không có | `args.ts:126-131`; `interactive-mode.ts:3233` | G1 |
| Nhiều phiên song song/tương tác | ACP sessions | RPC sessions + client/server packages | `acp_agent.py:73-85` | `packages/server`, `packages/client` | G1 |
| Trust project | Không | `/trust` + trust store | không có | `docs/security.md:59-82` | G2 |
| TUI 2 chế độ | Không | regular/fullscreen | không có | `args.ts:225-238` | G1 |

---

## 5. Ưu tiên

1. **P0 — Fork/branch session + export** (G3/G2): hiện session QQcode là file JSON phẳng; muốn tree cần đổi format sang JSONL append (xem doc 07). Nếu chưa làm tree ngay, tối thiểu thêm `/export` (HTML/markdown) và `--fork` bằng cách copy file session + cắt messages.
2. **P1 — Prompt templates** (G2): chi phí thấp, tận dụng cơ chế `PROMPT_DIR` đã có (`config.py:60`), thêm loader `~/.qqcode/prompts/*.md` → slash command.
3. **P1 — RPC/JSON mode** (G3): ACP đã có protocol riêng; thêm chế độ JSONL ổn định (như `--output vscode`) đã gần đủ. Việc còn lại là tài liệu hoá schema + lệnh điều khiển (compact, set_model, abort) qua stdin như Pi.
4. **P2 — `/model` trực tiếp + scoped models + thinking level runtime** — phụ thuộc doc 01.
5. **P2 — keybindings + theme JSON** để đạt parity UX.

> **Counterfactual**: bỏ `--output vscode`/ACP, QQcode mất toàn bộ khả năng tích hợp ngoài TUI; Pi bỏ RPC vẫn còn SDK + JSON mode. Đây là khác biệt về "programmability surface".
