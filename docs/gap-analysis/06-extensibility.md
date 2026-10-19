# 06 — Extensibility: Skills, MCP, Extensions, Packages

> Nguồn: **QQcode** `@ be6a96c` · **Pi** `@ 636703a0a`. Bằng chứng dạng `file:line`.

## 1. Tóm tắt điều hành

QQcode có 4 cơ chế mở rộng: **skills** (`.qqcode/skills/*/SKILL.md`), **custom tools** (file `.py` drop-in), **custom agents/prompts** (TOML/MD), và **MCP** (Python SDK: stdio + streamable HTTP). Không có extension API (hooks/events/commands/UI), không có package manager, không có theme/template chia sẻ.

Pi có 5 tầng mở rộng: **extensions** (~40 event hook + registerTool/Command/Shortcut/Flag/Provider/McpServer/VirtualModel/renderer), **skills** (Agent Skills spec + progressive disclosure), **prompt templates**, **themes**, và **Pi packages** (npm/git/local bundling cả 4 loại resource; `pi install/list/remove/update/config`).

Khoảng cách lớn nhất: Pi mở rộng được **mọi thứ runtime mà không cần sửa core**; QQcode muốn thêm capability mới phải sửa core hoặc viết custom tool theo BaseTool.

### Bảng điểm trục Extensibility

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| E1 | Extension API hooks/events | Không | ~40 event + register* | G3 |
| E2 | Custom tool drop-in | Có (BaseTool .py) | registerTool + examples | G0 |
| E3 | Skills frontmatter | name/description | + disable-model-invocation, Agent Skills spec | G1 |
| E4 | Skills locations | `.qqcode/skills` + `~/.qqcode/skills` | + `.agents/skills` (cwd→root), `~/.agents/skills`, package, CLI | G1 |
| E5 | Skill invocation | tool `skill` | `read` + `/skill:name` | G1 |
| E6 | MCP transports | stdio, streamable-http, http | stdio, streamable-http | G0 |
| E7 | MCP OAuth | Không | Có (PKCE + CIMD + mcp-auth.json) | G2 |
| E8 | MCP resources/prompts | Không (chỉ tools) | resources yes, prompts không | G2 |
| E9 | MCP CLI quản lý | Không | `pi mcp add/remove/list/login/logout` | G2 |
| E10 | MCP tool exposure control | Chỉ enable/disable pattern | exposure codemode/deferred/direct/hidden | G2 |
| E11 | Prompt templates như extension | Không | Có | G2 |
| E12 | Themes | Textual built-in + chọn | JSON schema + hot reload + light/dark pair | G1 |
| E13 | Packages (bundle & share) | Không | `pi install npm:@x` / git / local | G3 |
| E14 | Custom provider qua extension | Chỉ config.toml | `registerProvider` runtime | G2 |
| E15 | UI mở rộng (widget/renderer) | Textual widget nội bộ | registerMessageRenderer/EntryRenderer/ToolRenderer + custom TUI | G2 |
| E16 | Custom commands từ config | Không | `registerCommand` + template + skill | G2 |

---

## 2. QQcode hiện có gì

### 2.1 Skills

- Discovery: `.qqcode/skills` (đi ngược ancestor, dừng ở cái đầu) + `~/.qqcode/skills` (`skills/manager.py:64-87`).
- Mỗi skill là thư mục có `SKILL.md`; frontmatter parse bằng regex `^(\w+):\s*(.+)$` (`manager.py:89-116`), bắt buộc `name` + `description`, trùng tên thì project thắng (`:143-167`).
- Prompt listing `<available_skills>` không có location (`manager.py:208-224`).
- Tool `skill(name)` đọc full nội dung SKILL.md trả về model (`tools/builtins/skill.py:67-98`), có prompt riêng `prompts/skill.md`.
- Không hỗ trợ `allowed-tools`, `disable-model-invocation`, version, hay package skill. Không có `/skill:` command.

Ví dụ skill thực tế trong repo: `.qqcode/skills/pdf/` (SKILL.md + reference.md + forms.md + scripts/*.py) — tương thích Claude Skills ở mức cơ bản.

### 2.2 Custom tools

- Drop `.py` vào `tool_paths`, `.qqcode/tools`, `~/.qqcode/tools` (`manager.py:58-85`); mỗi class con `BaseTool` được discover (`manager.py:87-120`).
- Tool tự động có JSON schema từ `run(args)` Pydantic (`base.py:205-258`), prompt `.md` cùng tên (`base.py:116-135`), config riêng từ `config.toml [tools.<name>]` (`manager.py:236-255`).
- Giới hạn: không có hook/event, không đăng ký command/UI, không nhận context ngoài config + state, không có sandbox.
- Ví dụ grep tool có `use_default_ignore`, `codeignore_file` (`.qqcodeignore`) (`grep.py:69-72`, `141-148`).

### 2.3 MCP

- Dùng package `mcp>=1.14.0` (pyproject.toml:33).
- Config 3 loại transport (`config.py:222-246`): `MCPHttp`, `MCPStreamableHttp` (url/headers/api_key_env), `MCPStdio` (command/args). Discriminated union `MCPServer` (`config.py:244-246`), validate tên alias (`config.py:173-178`), header auth theo env (`config.py:208-219`).
- Discovery + proxy class runtime (`tools/mcp.py:102-313`), tên tool `{alias}_{remote.name}` (`mcp.py:142`, `245`), hint từ `server.prompt` (`config.py:169-171`).
- `_integrate_mcp` chạy **đồng bộ ngay khi khởi tạo ToolManager** (`manager.py:145-171`) — mỗi lần tạo Agent lại connect/list tools (không cache/log).
- Không hỗ trợ: MCP resources, prompts, OAuth, reconnect, timeout cấu hình, exposure control, `mcp add/list/login` CLI.

### 2.4 Custom agent / prompt

- `~/.qqcode/agents/NAME.toml` override config khi `--agent NAME` (`config.py:753-764`; `entrypoint.py:92-97`, `217`).
- Custom system prompt `~/.qqcode/prompts/<id>.md` (`config.py:494-503`).
- Không có extension/chia sẻ; không có package install.

### 2.5 ACP như kênh tích hợp

`qqcode-acp` cho phép client bên ngoài (Zed, v.v.) dùng QQcode qua agent-client-protocol (`pyproject.toml:69`; `acp/acp_agent.py:81+`; `distribution/zed/extension.toml`). Đây là kiểu tích hợp khác Pi (RPC/SDK/plugin).

---

## 3. Pi có gì

### 3.1 Extension API

- Kiểu API lớn: `ExtensionAPI` với `registerTool` (`types.ts:1634`), `registerCommand` (`:1643`), `registerShortcut`/`registerFlag` (`:1646`, `:1655`), `registerMessageRenderer`/`registerEntryRenderer`/`registerToolRenderer` (`:1678-1687`), `sendMessage`/`appendEntry` (`:1694-1710`), `exec` (`:1726`), `getActiveTools/setActiveTools` (`:1729-1741`), `setModel/getThinkingLevel` (`:1754-1763`), `registerProvider` (`:1821-1837`), `registerMcpServer` (`:1857-1863`), `registerVirtualModel` (`:1874-1877`), event bus (`:1880`).
- ~40 event: `before_agent_start`, `context`, `context_with_system`, `before_provider_request`, `provider_stream_event`, `tool_call`, `tool_result`, `message_*`, `session_before_compact`/`session_compact`, `session_shutdown`, `project_trust`, ... (`types.ts:682-1231`).
- Loader jiti cho TS/JS, discovery `.pi/extensions` + agent dir + packages (`loader.ts`, `package-manager.ts:563-609`); load ở mọi mode (`docs/extensions.md:248-251`).
- 80 example extension trong repo (`pi/packages/coding-agent/examples/extensions/`): permission-gate, protected-paths, sandbox, plan-mode, subagent, todo, git-checkpoint, custom provider, custom compaction...

### 3.2 Skills (Agent Skills spec)

- Frontmatter: `name`, `description`, `disable-model-invocation` (`skills.ts:67-72`, `341`); validation tên/description (`:11-14`, `92-127`).
- Locations: `<agentDir>/skills`, `<cwd>/.pi/skills`, `~/.agents/skills`, mọi `.agents/skills` từ cwd lên root, package, CLI (`package-manager.ts:450-487`, `2458-2561`).
- Prompt XML đầy đủ name/description/location + luật resolve relative path (`skills.ts:358-388`).
- Invocation: `read` tool hoặc `/skill:name` (`agent-session.ts:2145-2169`).
- Docs: `docs/skills.md`.

### 3.3 Dogfooding: thư mục `.pi/` của chính repo Pi

Pi dùng chính cơ chế extension/skill/prompt-template của mình để vận hành repo (đây là bằng chứng mạnh nhất cho tính mở rộng):

- `.pi/skills/` — 3 skill dạng file `.md` trực tiếp: `add-llm-provider.md` (checklist thêm provider, có frontmatter `name`/`description`), `interactive-testing.md`, `release.md` (`pi/.pi/skills/add-llm-provider.md:1-5`). Lưu ý Pi hỗ trợ skill là file `.md` trực tiếp, không bắt buộc thư mục `SKILL.md`.
- `.pi/prompts/` — prompt templates có frontmatter `description`/`argument-hint` và dùng `$@` (ví dụ `pr.md:1-4`), trở thành slash command trong repo.
- `.pi/extensions/` — extension TypeScript nhỏ hook event vòng đời (ví dụ `tps.ts` dùng `pi.on("agent_start"|"agent_end", ...)` và `ctx.hasUI`), `prompt-url-widget.ts`, `import-repro.ts`, `redraws.ts`.
- `.pi/git/`, `.pi/npm/` — thư mục cache/package-local (gitignore) minh hoạ cơ chế package local.

QQcode không có tương đương: repo `qqcode` chỉ có `.qqcode/skills/pdf/` (1 skill ví dụ copy từ Claude), không có prompt-template, không có extension hook.

### 3.4 Packages & themes

- Package = npm/git/local directory với `extensions/ skills/ prompts/ themes/` hoặc manifest `pi` (`docs/packages.md:44-74`); cài bằng `pi install/remove/list/update`, lưu settings, `pi config` bật/tắt resource (`package-manager-cli.ts:294-531`).
- Theme: `system/dark/light`, JSON schema, hot reload, light/dark pair (`docs/themes.md`).

### 3.5 MCP

- Client package độc lập, không phụ thuộc SDK MCP chính thức (`pi/packages/mcp/README.md:3`).
- Transport: stdio, streamable-http, in-memory test; **không SSE** (`docs/mcp.md:64-79`).
- OAuth: discovery + PKCE + callback server + CIMD; token `~/.pi/agent/mcp-auth.json` (`docs/mcp.md:115-187`).
- Resources: 3 tool `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` (`docs/mcp.md:236-248`); MCP prompts **không** hỗ trợ.
- Tool naming `mcp__<server>__<tool>` + hash khi trùng (`extensions/mcp/tools.ts:79-89`); exposure `codemode|deferred|direct|hidden` (`docs/mcp.md:189-204`).
- CLI `pi mcp add/remove/list/login/logout` (`extensions/mcp/cli.ts:33-73`).
- Kết quả >20KB bị middle-truncate + temp file (`extensions/mcp/tools.ts:51`, `122-139`).

---

## 4. Bảng gap chi tiết

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| Hook vòng đời (agent/tool/context) | không | ~40 events | không có | `extensions/types.ts:682-1231` | G3 |
| Đăng ký command/shortcut từ extension | không | có | không có | `types.ts:1643-1655` | G2 |
| Đăng ký provider runtime | không (chỉ config) | `registerProvider` | `config.py:157-165` | `types.ts:1821-1837` | G2 |
| UI renderer tuỳ biến | không | message/entry/tool renderer | không có | `types.ts:1678-1687` | G2 |
| Package bundle & share | không | npm/git/local + `pi install` | không có | `docs/packages.md`; `package-manager-cli.ts:294-531` | G3 |
| Theme chia sẻ | không (chỉ built-in Textual) | JSON + hot reload | `config_app.py:15` | `docs/themes.md` | G1 |
| MCP OAuth | không | có | `config.py:181-219` (chỉ header/env) | `docs/mcp.md:115-187` | G2 |
| MCP resources | không | có (3 tool) | `mcp.py` chỉ tools | `docs/mcp.md:236-248` | G2 |
| MCP CLI | không | `pi mcp ...` | không có | `extensions/mcp/cli.ts:33-73` | G2 |
| MCP discovery cache/reconnect | không (mỗi Agent connect lại) | list thay đổi được xử lý, timeout 60s | `manager.py:145-171` | `docs/mcp.md:100` | G2 |
| Skill `disable-model-invocation` | không | có | `manager.py:140-141` | `skills.ts:341` | G1 |
| Skill location trong prompt | không | có | `manager.py:217-224` | `skills.ts:374-387` | G1 |
| `.agents/skills` compat | không | có | `manager.py:76-84` | `package-manager.ts:450-487` | G1 |
| `/skill:name` | không | có | không có | `agent-session.ts:2145-2169` | G1 |
| Prompt templates đóng gói | không | có | không có | `prompt-templates.ts` | G2 |
| Custom tool không cần Pydantic base | một phần (BaseTool bắt buộc) | `registerTool` với schema bất kỳ | `base.py:93-98` | `types.ts:1634` | G1 |
| MCP exposure mode | không | 4 mode | không có | `docs/mcp.md:189-204` | G2 |

---

## 5. Ưu tiên

1. **P0 — Event/hook API tối thiểu** (G3): thêm một registry đơn giản trong `vibe/core/` (`hooks.py`) phát các event `agent_start`, `before_llm_request`, `tool_call`, `tool_result`, `agent_end`; cho custom tool/agent đăng ký qua entry point hoặc file trong `.qqcode/hooks/`. Không cần bằng Pi, nhưng là nền cho mọi mở rộng sau.
2. **P1 — MCP hardening** (G2): cache discovery theo server + tool list động; thêm `timeout`, `enabled`, resources (3 tool như Pi); thêm CLI `qqcode mcp list/login`.
3. **P1 — Skills parity** (G1): `location` trong prompt listing, `disable-model-invocation`, `.agents/skills` compat, `/skill:name`.
4. **P2 — Resource packaging**: định nghĩa `qqcode` manifest trong git repo (`tools/`, `skills/`, `prompts/`, `agents/`) và lệnh `qqcode install <git-url>` — chưa cần npm registry.
5. **P2 — Prompt templates & commands động**: tái dùng `CommandRegistry` (`commands.py:14-82`) để nạp thêm command từ file.

> **Counterfactual**: nếu không có event API, mọi nhu cầu như "permission theo path", "auto-commit", "plan mode", "subagent" đều phải fork core — trong khi Pi giải quyết toàn bộ bằng extension ví dụ sẵn có.
