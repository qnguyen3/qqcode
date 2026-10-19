# Plan 07 — Commands & CLI Surface

> Gap: C2, C3, C4, C6, C7, C8, C9, C13, C14, S5 · Mức độ: G3×2, G2×7, G1×1 · Wave 2

## 1. Mục tiêu

1. Bổ sung slash command thiết yếu: `/model`, `/thinking`, `/new`, `/resume`, `/export`, `/import`, `/fork`, `/session`, `/name`.
2. Model scoping (`enabled_models`) + hotkey cycle model/thinking.
3. Chế độ `--output events` ổn định (JSONL có schema + tài liệu) và **lệnh điều khiển qua stdin** (compact, abort, set_model).
4. CLI flag mới: `--system-prompt`, `--append-system-prompt`, `--no-context-files`, `--session-dir`, `--name`, `--no-session`, `--fork`, `--tools`/`--exclude-tools`.
5. Tận dụng plan 02 cho export/import/fork.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| 10 slash command; không có model/export/fork | `vibe/cli/commands.py:18-70` |
| Đổi model chỉ qua `/config` (ConfigApp) | `vibe/cli/textual_ui/widgets/config_app.py:56-82` |
| `--output` 4 format, VSCode format riêng | `vibe/core/types.py:281-286`; `output_formatters.py:85-244` |
| Không điều khiển runtime từ ngoài (không RPC) | không có; ACP là kênh riêng |
| Không export/import/fork | `vibe/core/interaction_logger.py` chỉ list/load |
| Flag thiếu | `entrypoint.py:27-146` |

## 3. Thiết kế

### 3.1 Slash commands mới

| Command | Hành vi | Phụ thuộc |
|---|---|---|
| `/model [alias]` | không tham số → mở selector (tái dùng logic ConfigApp); có alias → đổi ngay, ghi entry `model_change`, cập nhật pricing/stats | plan 05 |
| `/thinking [level]` | đổi thinking level runtime (plan 06); hiển thị level hiện tại + capability | plan 06 |
| `/new` | session mới (messages reset, session_id mới, giữ config) | plan 02 |
| `/resume [id]` | picker session (tái dùng `ConversationsApp`); có id → resume trực tiếp | plan 02 |
| `/name [tên]` | đặt tên session, lưu `CustomEntry(key="name")` + header | plan 02 |
| `/session` | in info session: id, tên, model, context %, cost, số entry | plan 02/05/10 |
| `/export [path]` | mặc định markdown; `.jsonl` xuất raw entries; `.html` xuất HTML đơn giản (tự render, không template ngoài) | plan 02 |
| `/import <path>` | thay session hiện tại bằng file (`.jsonl`/`.json` legacy) | plan 02 |
| `/fork` | chọn user message gần đây → fork tại entry tương ứng, chuyển session | plan 02 |
| `/copy` | copy message assistant cuối vào clipboard | — |
| `/help` | cập nhật danh sách động (build-in + template + skill) | plan 04 |

Cơ chế mở rộng `CommandRegistry`: thêm constructor nhận `dynamic_commands: list[Command]` (template/skill) để `/help` và autocomplete hiển thị đủ.

### 3.2 Model scoping & hotkey

- Config `enabled_models: list[str] = []` (glob alias, ví dụ `["claude-*", "zai/*"]`); rỗng = tất cả.
- `/scoped-models` mở màn hình chọn (tái dùng ConfigApp list) lưu vào config.
- Hotkey: `Ctrl+P` cycle model kế tiếp trong scope; `Shift+Ctrl+P` ngược (Textual binding mới trong `app.py`).
- `Shift+Tab` hiện đang toggle mode; giữ nguyên. Thinking cycle để `Ctrl+T`? — `Ctrl+T` đang là todo view; dùng `Ctrl+Y` cho thinking cycle (cập nhật `commands.py` help text).
- Ghi `model_change`/`thinking_level_change` entry + custom entry để resume khôi phục.

### 3.3 Chế độ `--output events` + control commands

Chuẩn hoá event stream:

```jsonc
{"type":"session.started","session_id":"...","timestamp":"..."}
{"type":"turn.started","turn":1}
{"type":"message.delta","item_id":"...","delta":"...","content":"..."}
{"type":"tool.call","tool_call_id":"...","tool_name":"bash","args":{...}}
{"type":"tool.result","tool_call_id":"...","status":"ok|error|skipped","text":"...","truncated":false}
{"type":"turn.completed","usage":{"input":1,"output":2,"cache_read":0},"cost_usd":0.001}
{"type":"session.compacted","tokens_before":...,"tokens_after":...}
{"type":"error","message":"..."}
```

- `EventJsonlFormatter` mới dùng `StreamEventType` mở rộng; giữ `--output vscode` cũ để không phá VSCode extension (mapping từ event mới nếu cần).
- Control commands qua stdin khi `--output events` (chế độ programmatic interactive):
  ```jsonc
  {"type":"compact","instructions":"..."}
  {"type":"abort"}
  {"type":"set_model","alias":"..."}
  {"type":"set_thinking","level":"high"}
  {"type":"steer","message":"..."}       // chèn chỉ dẫn vào lượt đang chạy
  ```
  Đọc bằng task `stdin_reader` song song với agent loop; dispatch vào `Agent` qua API mới (`agent.abort()`, `agent.steer(text)`).
- Tài liệu `docs/cli-events.md` mô tả schema + ví dụ; version field `"schema":1`.

### 3.4 `Agent.steer` / `abort`

- `Agent` thêm `self._steering: asyncio.Queue[str]`; `_conversation_loop` sau mỗi turn nếu có steer message → append user message và tiếp tục (không chờ user input).
- `abort()` set `asyncio.Event` + huỷ task hiện tại an toàn (đã có `_interrupt_agent` trong UI; tái dùng logic).

### 3.5 CLI flags mới

| Flag | Hành vi |
|---|---|
| `--system-prompt <text\|file>` | thay prompt hệ thống (đọc file nếu path tồn tại) |
| `--append-system-prompt <text\|file>` (lặp) | append vào section `append` |
| `--no-context-files` | tắt nạp context files (plan 04) |
| `--session-dir DIR` | ép thư mục session (override config) |
| `--name NAME` | đặt tên session |
| `--no-session` | không ghi session |
| `--fork [ID]` | fork session gần nhất/tại ID rồi mở |
| `--tools LIST` / `--exclude-tools LIST` | thay `enabled_tools`/`disabled_tools` (hỗ trợ `+name/-name/*`) |
| `--prompt-template DIR` (lặp) | thêm nguồn template (plan 04) |

Tương thích: `--enabled-tools` cũ giữ nguyên alias của `--tools`.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 7.1 | CommandRegistry động (template/skill/help) | `cli/commands.py` | M | plan 04 |
| 7.2 | `/model` + scoped models | `app.py`, `config_app.py`, `config.py` | M | plan 05 |
| 7.3 | `/thinking` + hotkey cycle | `app.py`, `config.py` | M | plan 06 |
| 7.4 | `/new`, `/resume`, `/name` | `app.py`, sessions | M | plan 02 |
| 7.5 | `/session` info | `app.py` | S | plan 02/10 |
| 7.6 | `/export` markdown/jsonl | `vibe/core/sessions/export.py` (mới), `app.py` | M | plan 02 |
| 7.7 | `/import` | `app.py`, sessions | S | plan 02 |
| 7.8 | `/fork` | `app.py`, sessions | M | plan 02 |
| 7.9 | EventJsonlFormatter + schema doc | `output_formatters.py`, `docs/cli-events.md` | M | — |
| 7.10 | stdin control commands + `Agent.steer/abort` | `programmatic.py`, `agent.py` | L | 7.9 |
| 7.11 | CLI flags mới + tests | `entrypoint.py` | M | plan 02/04 |
| 7.12 | Ctrl+P model cycle | `app.py` | S | 7.2 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/test_cli_programmatic_preload.py` | flag mới không phá luồng cũ |
| `tests/test_ui_commands.py` | `/model alias` đổi model + ghi entry; `/name`; `/session` |
| `tests/sessions/test_export.py` | markdown/jsonl export tương ứng history; import roundtrip |
| `tests/test_events_output.py` | schema JSONL: type bắt buộc, version; escape đúng |
| `tests/test_stdin_control.py` | `compact`/`abort`/`set_model` qua stdin phản hồi đúng |
| `tests/test_agent_steer.py` | steer chèn message giữa turn, agent tiếp tục đúng |
| `tests/test_ui_autocomplete.py` | autocomplete gồm command động |

## 6. DoD

- [ ] Tất cả slash command mới chạy trong UI và có test.
- [ ] `/export` + `/import` roundtrip; `/fork` resume được.
- [ ] `--output events` có tài liệu schema và test; VSCode extension hiện tại không vỡ (cập nhật nếu cần).
- [ ] Điều khiển `compact`/`abort`/`set_model` từ stdin hoạt động khi chạy programmatic.
- [ ] Flag cũ `--enabled-tools` vẫn chạy; flag mới có help text rõ ràng.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Event schema mới phá VSCode extension | giữ `--output vscode`; thêm `--output events` riêng |
| stdin control gây race với agent loop | mọi thay đổi qua asyncio queue xử lý ở đầu turn; abort qua event |
| `/fork` trên session legacy không có id | legacy reader sinh id ổn định (plan 02) |
| Hotkey trùng Textual | kiểm tra binding hiện có, cập nhật help |

Rollback: các command/flag độc lập; tắt bằng cách xoá đăng ký; event mode là opt-in.

## 8. Commit plan

```
feat(cli): switch models with a slash command and shortcut
feat(cli): set the thinking level at runtime
feat(sessions): export and import conversations
feat(sessions): fork a conversation from a previous message
feat(cli): emit a versioned jsonl event stream
feat(cli): accept control commands on stdin
feat(cli): add prompt and session flags
```
