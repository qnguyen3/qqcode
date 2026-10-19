# 04 — Prompt Engineering

> Nguồn: **QQcode** `@ be6a96c` · **Pi** `@ 636703a0a`. Bằng chứng dạng `file:line`.

## 1. Tóm tắt điều hành

QQcode xây system prompt bằng **nối chuỗi** (`"\n\n".join(sections)`) một lần khi khởi tạo Agent, dùng prompt `.md` cho hệ thống/tool/mode, cho phép custom prompt và custom agent TOML. Điểm mạnh: dễ đọc, prompt tool tách file, plan mode prompt đầy đủ.

Khác biệt kiến trúc với Pi nằm ở 3 điểm:

1. **Không có cấu trúc section**: khi đổi mode, QQcode **thay toàn bộ** `messages[0]` bằng prompt mới (`agent.py:1134-1146`) — phá prompt cache và làm model thấy system message "biến mất/tái xuất". Pi giữ prompt dạng `Record<section, text>`, diff và chỉ append patch (`system-prompt.ts:217-229`).
2. **Không có prompt templates** (`$1`, `$@`, `${N:-default}`) và không có `/skill:name`; Pi có cả hai.
3. **Context files đơn cấp**: QQcode chỉ đọc 1 file tại workdir (`system_prompt.py:30-37`); Pi đi từ agent dir + toàn bộ ancestor, hỗ trợ `AGENTS.override.md`, `CLAUDE.md`, worktree shadowing (`resource-loader.ts:184-270`).

Ngoài ra QQcode đưa "tên model" và "commit signature" vào system prompt (`system_prompt.py:365-375`, `390-391`) — điều Pi không làm; các chi tiết này làm prompt dài thêm và có thể gây nhiễu, nhưng đổi lại tăng tuân thủ commit convention. Pi thay bằng `docs` section định tuyến tài liệu (`system-prompt.ts:162-169`).

### Bảng điểm trục Prompt Engineering

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| R1 | Prompt dạng section + diff | Không (string concat, thay toàn bộ) | Có (`SystemPromptSections` + `diffSystemPromptSections`) | G2 |
| R2 | Prompt templates có tham số | Không | Có (`$1/$@/${N:-default}`) | G2 |
| R3 | Context files đa cấp | 1 file/workdir | agent dir + ancestors + override + worktree | G2 |
| R4 | Toggle context files | Không | `--no-context-files` | G1 |
| R5 | Custom system prompt runtime | File + agent TOML | `--system-prompt`, `--append-system-prompt` | G1 |
| R6 | Tool prompt tách file | Có (`prompts/*.md`) | Có (snippet + guidelines cạnh tool) | G0 |
| R7 | Skill prompt listing | Có, đơn giản hơn | XML đầy đủ + location + disable-model-invocation | G1 |
| R8 | Skill invocation | Tool `skill(name)` | `read` file (progressive disclosure) + `/skill:` | G0 (khác hướng) |
| R9 | Dynamic env info | OS + shell | cwd section (không OS/date/git) | G0 |
| R10 | Docs self-routing | Không | Section `docs` trỏ docs/examples | G1 |
| R11 | Prompt guideline theo tool | Có (prompt per tool) | `promptSnippet`/`promptGuidelines` + dedupe | G0 |
| R12 | Plan mode prompt | Có (`plan_mode.md` 52 dòng) | Ví dụ extension (không built-in) | G0 (QQcode nhỉnh hơn) |
| R13 | Commit signature prompt | Có, config bật/tắt | Không built-in (extension auto-commit) | G0 (khác hướng) |
| R14 | Prompt caching khi prompt đổi | Không (rewrite) | Có (diff patch) | G2 |

---

## 2. QQcode hiện có gì

### 2.1 Cấu trúc system prompt

`vibe/core/prompts/`:

| File | Vai trò | Nội dung |
|---|---|---|
| `cli.md` (13 dòng) | Prompt hệ thống mặc định `SystemPrompt.CLI` | "You are operating as and within QQcode..." + luật dùng tool |
| `tests.md` | Prompt thay thế cho test (`SystemPrompt.TESTS`) | — |
| `compact.md` (48 dòng) | `UtilityPrompt.COMPACT` — summary 7 mục | — |
| `plan_mode.md` (52 dòng) | `UtilityPrompt.PLAN_MODE` — luật plan mode + format plan + allowlist bash | — |
| `dangerous_directory.md` (5 dòng) | `UtilityPrompt.DANGEROUS_DIRECTORY` — thay project context khi thư mục nguy hiểm | — |
| `project_context.md` (8 dòng) | `UtilityPrompt.PROJECT_CONTEXT` — template `{structure}`, `{abs_path}`, `{git_status}` | — |

Enum & loader `vibe/core/prompts/__init__.py:11-29`: `Prompt.read()` đọc file `.md` theo tên enum; `SystemPrompt` gồm `CLI`, `TESTS`; `UtilityPrompt` gồm `COMPACT`, `DANGEROUS_DIRECTORY`, `PLAN_MODE`, `PROJECT_CONTEXT`.

Thứ tự ghép (`system_prompt.py:385-449`):

```
1. config.system_prompt (cli.md hoặc custom)
2. _add_commit_signature()          # include_commit_signature
3. "Your model name is: `{alias}`"  # include_model_info
4. plan_mode.md                     # nếu mode == "plan"
5. _get_os_system_prompt()          # "The operating system is {X} with shell `{Y}`" + Windows rules
6. tool prompts (mỗi tool 1 file .md, join bằng "\n---\n")
7. <available_skills>               # nếu có skill
8. user instructions (config.instructions hoặc ~/.qqcode/instructions.md)
9. project context (cây thư mục + git status) hoặc dangerous_directory
10. project doc (AGENTS.md | QQCODE.md | .qqcode.md)
11. additional directory contexts
```

Tất cả nối bằng `"\n\n".join(sections)` (`system_prompt.py:449`) — không có wrapper XML theo section, không có tên section.

### 2.2 Tool prompt

Mỗi tool có `.md` riêng cùng tên file `.py` trong `prompts/` (`tools/base.py:116-135`), hiện có: `bash.md`, `edit.md`, `grep.md`, `read_file.md`, `skill.md`, `todo.md`, `write_file.md`. Chúng được nhét vào system prompt của **mọi** tool active (`system_prompt.py:399-405`), không phải vào tool description. Nội dung khá dài (bash.md dài ~70 dòng, todo.md ~130 dòng) — đây là một lượng prompt lớn luôn hiện diện.

Tool `description` riêng vẫn tồn tại trên class (viết trong code) và là mô tả gửi cho API (`format.py:152-161`).

### 2.3 Custom prompt & custom agent

- `system_prompt_id` (default `"cli"`, `config.py:440`): nếu không khớp `SystemPrompt`, đọc `~/.qqcode/prompts/{id}.md` (`config.py:494-503`).
- Agent TOML `~/.qqcode/agents/<name>.toml` override config: `active_model`, `system_prompt_id`, `disabled_tools`, permissions... (`config.py:753-764`; README ví dụ `README.md:203-233`).
- User instructions file `~/.qqcode/instructions.md` (`config.py:61`, `system_prompt.py:23-27`).
- Project doc `AGENTS.md` / `QQCODE.md` / `.qqcode.md` **chỉ tại workdir** (`system_prompt.py:30-37`), cắt theo `max_doc_bytes = 32KB` (`config.py:125`).

### 2.4 Prompt đổi theo mode — vấn đề cache

`agent.py:197-202` khi mode đổi → `_regenerate_system_prompt_for_mode()` (`:1134-1146`) thay `self.messages[0]` bằng prompt mới. Với Anthropic/OpenAI prompt caching, system message là phần cache quan trọng nhất; thay toàn bộ khiến cache miss cho đến block cache cuối cùng. Ngoài ra mode plan ↔ interactive đổi thường xuyên trong luồng plan approval (`agent.py:663-683`).

### 2.5 Skills trong prompt

`SkillManager.get_skills_prompt_section` (`skills/manager.py:208-224`) sinh:

```
<available_skills>
- name: pdf
  description: ...
</available_skills>
```

Thiếu `location` (đường dẫn file) — model biết tên skill nhưng không biết file ở đâu cho tới khi gọi tool `skill` (tool tự đọc `skill_manager.get_skill_content`, `skill.py:84-94`). Đây là "progressive disclosure" một bước; Pi disclosure tương tự nhưng đưa `location` để model tự `read`, kèm luật resolve relative path (`skills.ts:365-387`).

### 2.6 Không có prompt templates

Không có loader `prompts/*.md` nào biến thành slash command; `PROMPT_DIR` (`config.py:60`) chỉ dùng cho system prompt. Không có cơ chế tham số.

---

## 3. Pi có gì

### 3.1 Prompt dạng section + diff

- `buildSystemPromptSections` trả `Record<section, text>`; section `preamble` không bọc, các section khác bọc `<name>...</name>` (`system-prompt.ts:128-193`).
- Thứ tự: `preamble` → `tools` → `rules` → `docs` → `addendum` → `project_context` → `skills` → `cwd` → custom sections từ extension (`system-prompt.ts:151-186`).
- `diffSystemPromptSections` so với section đã replay trong transcript, chỉ trả patch thay đổi (`system-prompt.ts:217-229`); `agent-session.ts:1736-1748` dùng để append tối thiểu vào transcript — cache prefix được giữ.
- Extension có thể chèn section hoặc force prompt qua `before_agent_start` (`agent-session.ts:1750-1763`).

### 3.2 Rules động & guideline

`buildRules` dedupe guideline từ tool + prompt + luật mặc định, có luật fallback khi thiếu grep/find/ls (`system-prompt.ts:88-125`). Tool đóng góp `promptSnippet` + `promptGuidelines` (ví dụ `tools/read.ts:20-23`), được render vào section `tools`/`rules` (`system-prompt.ts:157-161`).

### 3.3 Context files & system prompt files

- Tên file theo thứ tự ưu tiên: `AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`, `CLAUDE.md`, `CLAUDE.MD` (`resource-loader.ts:184-203`).
- Nạp từ agent dir trước, rồi đi ngược ancestor từ cwd, có xử lý git worktree shadowing (`resource-loader.ts:205-270`).
- Bọc `<project_instructions path="...">` (`system-prompt.ts:79-86`).
- `SYSTEM.md` (project thắng global) thay prompt, `APPEND_SYSTEM.md` nối thêm (`resource-loader.ts:1209-1235`).
- Load kể cả khi project chưa trust (`docs/configuration.md:47`), tắt bằng `--no-context-files` (`resource-loader.ts:642-649`).

### 3.4 Prompt templates

`prompt-templates.ts:25-103` hỗ trợ `$1…`, `$@`/`$ARGUMENTS`, `${N:-default}`, `${@:-default}`, `${@:N}`, `${@:N:L}` với parse quoted args; file `.md` trong `<agentDir>/prompts` và `<cwd>/.pi/prompts`; tên file thành slash command (`:129`, `:304-320`); expansion xảy ra trước khi gửi (`agent-session.ts:1971-2008`).

### 3.5 Skills prompt & invocation

`formatSkillsForPrompt` (`skills.ts:358-388`) sinh XML có `name/description/location`; chỉ hiện khi có tool reader (`read`/`bash`) active (`system-prompt.ts:174-182`); skill `disable-model-invocation` bị ẩn khỏi prompt nhưng vẫn gọi được qua `/skill:name` (`agent-session.ts:2145-2169`).

### 3.6 Prompt khác biệt đáng chú ý

- Pi **không** nhét OS/shell/date/git vào prompt mặc định (git branch chỉ hiện ở footer — `footer-data-provider.ts:127-132`).
- Pi có `docs` section tự định tuyến docs/examples (`system-prompt.ts:162-169`) — model biết đọc tài liệu ở đâu khi được hỏi về Pi.

---

## 4. Bảng gap chi tiết

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| Section prompt có tên | không | `SystemPromptSections` | `system_prompt.py:449` | `system-prompt.ts:56`, `188-192` | G2 |
| Diff prompt khi đổi mode | thay toàn bộ `messages[0]` | patch section tối thiểu | `agent.py:1134-1146` | `system-prompt.ts:217-229` | G2 |
| Context file hierarchy | 1 file workdir | agent dir + ancestor + override + worktree | `system_prompt.py:30-37`; `config.py:63` | `resource-loader.ts:184-270` | G2 |
| CLAUDE.md compat | không | có | `config.py:63` (`.qqcode.md`) | `resource-loader.ts:191-193` | G1 |
| Prompt template | không | có, có tham số | không có | `prompt-templates.ts:25-103` | G2 |
| Skill location trong prompt | không | có | `manager.py:217-224` | `skills.ts:374-387` | G1 |
| Ẩn skill khỏi model | không | `disable-model-invocation` | không có | `skills.ts:341`, `359` | G1 |
| System prompt override CLI | không | `--system-prompt`/`--append-system-prompt` | không có | `args.ts:121-125` | G1 |
| Docs self-routing | không | section `docs` | không có | `system-prompt.ts:162-169` | G1 |
| Extension chèn section | không | `sections` option + hook | không có | `system-prompt.ts:29-30`, `184-186` | G2 |
| Prompt guideline động | prompt tool tĩnh | guideline theo tool + dedupe + fallback | `system_prompt.py:399-405` | `system-prompt.ts:88-125` | G1 |

---

## 5. Ưu tiên

1. **P0 — Section hoá system prompt + diff**: định nghĩa `PromptSection` (name, content, tag) trong `vibe/core/system_prompt.py`; so sánh với prompt đã gửi và chỉ thay phần đổi. Đây là điều kiện để prompt caching hoạt động khi mode đổi và khi compact/reload.
2. **P1 — Prompt templates**: loader `~/.qqcode/prompts/commands/*.md` + thay thế `$1/$@`; đăng ký vào `CommandRegistry` (`commands.py`) động.
3. **P1 — Context file hierarchy**: mở rộng `_load_project_doc` đi ngược ancestor, thêm `AGENTS.override.md`, `CLAUDE.md`; giữ `max_doc_bytes` cho từng file và tổng.
4. **P2 — Skill prompt parity**: thêm `location` vào listing; hỗ trợ `disable-model-invocation`; thêm `/skill:name` command.
5. **P2 — Giảm prompt phình**: tách `include_prompt_detail` (đã có, `config.py:444`) thành từng nhóm (tool prompts riêng), và chuyển các đoạn dài trong `todo.md`/`bash.md` thành guideline ngắn.

> **Counterfactual**: nếu vẫn thay `messages[0]` toàn bộ mỗi lần vào/ra plan mode, mọi cache system+history của Anthropic/OpenAI sẽ miss đúng tại các bước chuyển mode — chi phí tăng mạnh ở đúng luồng plan mode được quảng cáo.
