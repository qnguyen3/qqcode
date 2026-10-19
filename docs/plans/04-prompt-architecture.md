# Plan 04 — Prompt Architecture: sections, context files, templates

> Gap: R1, R2, R3, R14, E3, E5 · Mức độ: G2×4, G1×2 · Wave 1

## 1. Mục tiêu

1. System prompt dựng từ các **section có tên**, hỗ trợ cập nhật từng phần và giữ prefix ổn định cho prompt caching.
2. Context files nạp theo cấp thư mục (agent dir + ancestors), hỗ trợ override, tương thích `AGENTS.md`/`CLAUDE.md`.
3. Prompt templates có tham số (`$1`, `$@`, `${N:-default}`) trở thành slash command.
4. Skill listing đầy đủ (có `location`, `disable-model-invocation`), thêm lệnh `/skill:name`.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Prompt nối chuỗi, không section | `vibe/core/system_prompt.py:378-449` |
| Đổi mode → thay toàn bộ `messages[0]` | `vibe/core/agent.py:1134-1146` |
| Context file: 1 file tại workdir | `system_prompt.py:30-37`; `vibe/core/config.py:63` |
| Không prompt template | không có loader; `PROMPT_DIR` chỉ cho system prompt (`config.py:60`) |
| Skill listing thiếu location | `vibe/core/skills/manager.py:217-224` |
| Skill invocation chỉ qua tool | `vibe/core/tools/builtins/skill.py:67-98` |

## 3. Thiết kế

### 3.1 Section model (`vibe/core/prompts/sections.py`)

```python
@dataclass(frozen=True)
class PromptSection:
    name: str                       # ^[a-z][a-z0-9_-]*$
    content: str
    tagged: bool = True             # bọc <name>...</name>
    cache_stable: bool = True       # xếp vào vùng đầu để tối ưu cache

class PromptSections:
    """Ánh xạ name -> content, giữ thứ tự chèn. render() sinh text cuối cùng."""
    def set(self, section: PromptSection) -> None: ...
    def remove(self, name: str) -> None: ...
    def render(self) -> str: ...
    def diff(self, other: "PromptSections") -> dict[str, str | None]: ...
```

Thứ tự cố định (cache_stable trước, động sau cùng):

```
preamble            (config.system_prompt, cli.md/custom)     stable
commit_signature    (nếu bật)                                 stable
tool_prompts        (prompt .md của tool active)              stable
skills              (<available_skills>)                      stable theo skills
model_info          "Your model name is: ..."                 stable theo session
docs_routing        (tài liệu QQcode — mới)                   stable
project_context     (cây thư mục + git status)                stable theo session
project_files       (<project_instructions path=...>)          stable theo session
instructions        (user instructions)                        stable
mode                (plan_mode.md khi ở plan)                  ĐỘNG
os_env              (OS + shell)                               stable
append              (append từ CLI/config)                     stable
```

Chỉ section `mode` đổi khi vào/ra plan; vì nằm gần cuối, prefix cache (preamble→instructions) được giữ.

### 3.2 Áp dụng vào Agent

- `self._prompt_sections: PromptSections` là nguồn sự thật; `messages[0].content = sections.render()`.
- Khi mode đổi: chỉ set/remove section `mode`, rồi nếu `render()` khác `messages[0]` thì cập nhật. Thêm event `SystemPromptChangedEvent` cho UI/debug.
- Khi tool loadout đổi (plan mode, enabled_tools): cập nhật `tool_prompts` + `skills`.
- Hook cho tương lai (plan 08): `before_prompt_build` cho phép chỉnh `PromptSections`.

### 3.3 Context files hierarchy (`vibe/core/prompts/context_files.py`)

```python
CONTEXT_FILENAMES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "QQCODE.md", ".qqcode.md", "CLAUDE.md", "CLAUDE.MD")

def load_context_files(workdir: Path, agent_dir: Path, *, max_bytes_per_file: int, max_total_bytes: int) -> list[ContextFile]:
    """1) file trong agent_dir (global) trước
       2) đi từ workdir lên parent, mỗi thư mục lấy file đầu tiên khớp
       3) khử trùng lặp theo nội dung; cắt mỗi file theo max_bytes_per_file
       4) nếu tổng vượt max_total_bytes: cắt bớt từ file xa nhất, ghi chú (truncated)"""
```
- Render:
  ```
  <project_instructions path="/abs/path/AGENTS.md">
  ...nội dung...
  </project_instructions>
  ```
- Thêm config `include_context_files: bool = True`; CLI `--no-context-files` (plan 07).
- Giữ `_load_project_doc` cũ làm fallback khi tính năng tắt.

### 3.4 Prompt templates (`vibe/core/prompts/templates.py`)

```python
@dataclass(frozen=True)
class PromptTemplate:
    name: str                       # tên file không đuôi
    description: str
    body: str
    source: Path

def load_templates(dirs: list[Path]) -> dict[str, PromptTemplate]: ...
def expand(body: str, args: list[str]) -> str:
    """$1..$9, $@/`$ARGUMENTS`, ${N:-default}, ${@:-default}, ${@:N}, ${@:N:L}
    với parse args kiểu shell (quote đơn/kép)."""

def render_template(t: PromptTemplate, raw_args: str) -> str: ...
```
- Nguồn: `~/.qqcode/prompts/commands/*.md`, `.qqcode/prompts/commands/*.md`, `--prompt-template DIR` (plan 07).
- Frontmatter YAML đơn giản: `description`, `argument-hint` (parse bằng regex như skill hiện tại để không thêm dependency).
- Đăng ký động vào `CommandRegistry`: mỗi template là `/<name>`; handler expand rồi gửi như user message.
- Kiểm tra trùng tên với built-in: template thua, log cảnh báo.

### 3.5 Skill parity

- `SkillInfo` thêm `disable_model_invocation: bool`, `allowed_tools: list[str] | None` (chỉ parse, chưa enforce ở plan này).
- `get_skills_prompt_section`:
  ```xml
  <available_skills>
    <skill>
      <name>pdf</name>
      <description>...</description>
      <location>/abs/.qqcode/skills/pdf/SKILL.md</location>
    </skill>
  </available_skills>
  ```
  Kèm câu hướng dẫn "read the file at location when the task matches" + resolve relative path theo thư mục skill.
- Skill `disable-model-invocation` bị ẩn khỏi prompt; chỉ gọi được qua `/skill:name`.
- `/skill:<name> [args]`: đọc SKILL.md, bỏ frontmatter, bọc `<skill name="..." location="...">`, nối args; gửi như user message.
- Hỗ trợ thêm nguồn `.agents/skills` (cwd → root) và `~/.agents/skills`.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 4.1 | `PromptSection`/`PromptSections` + render/diff + test | `vibe/core/prompts/sections.py` (mới) | M | — |
| 4.2 | Chuyển `get_universal_system_prompt` sang sections | `system_prompt.py` | L | 4.1 |
| 4.3 | Agent chỉ cập nhật section `mode` khi đổi mode | `agent.py` | M | 4.2 |
| 4.4 | Context files hierarchy + render | `vibe/core/prompts/context_files.py` (mới) | M | — |
| 4.5 | Prompt templates loader + expander | `vibe/core/prompts/templates.py` (mới) | M | — |
| 4.6 | Đăng ký template vào CommandRegistry + handler | `cli/commands.py`, `textual_ui/app.py` | M | 4.5 |
| 4.7 | Skill frontmatter mở rộng + listing XML + `.agents/skills` | `skills/manager.py` | M | — |
| 4.8 | `/skill:name` command + handler | `cli/commands.py`, `app.py` | S | 4.7 |
| 4.9 | Doc `docs/prompt-templates.md`, cập nhật README | docs | S | 4.6 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/prompts/test_sections.py` | render đúng tag; diff chỉ trả phần đổi; section name validation |
| `tests/test_system_prompt.py` | cập nhật: cùng nội dung với hành vi cũ ở mode interactive |
| `tests/test_plan_mode_system_prompt.py` | đổi mode chỉ đổi section `mode`, phần còn lại giữ nguyên (so sánh chuỗi) |
| `tests/prompts/test_context_files.py` | ancestor walk; override; CLUADE.md fallback; cắt theo byte |
| `tests/prompts/test_templates.py` | `$1`, `$@`, `${N:-default}`, ${@:N:L}; quote; thiếu arg |
| `tests/skills/test_skill_manager.py` | disable-model-invocation ẩn khỏi prompt; location có trong XML |
| `tests/test_ui_skill_command.py` | `/skill:name` mở rộng đúng, nối args |

## 6. DoD

- [ ] Prompt ở mode interactive giống hệt về mặt nội dung so với trước (regression snapshot có chủ đích).
- [ ] Vào/ra plan mode không thay đổi các section trước `mode` (test so sánh prefix).
- [ ] `AGENTS.override.md` ưu tiên hơn `AGENTS.md`; ancestor hoạt động; `CLAUDE.md` được nhận.
- [ ] Template `pr.md` với `$1` chạy được end-to-end trong test UI.
- [ ] Skill listing có `location`; skill `disable-model-invocation` không xuất hiện trong prompt nhưng `/skill:` vẫn chạy.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Section refactor làm lệch prompt ngoài ý muốn | snapshot test prompt trước/sau, review diff có chủ đích |
| Cache không cải thiện vì thứ tự section | đặt dynamic cuối; đo `usage.cache_read` trước/sau nếu provider trả |
| Parser frontmatter YAML dễ vỡ | dùng regex hiện có, không thêm PyYAML |
| `.agents/skills` trùng với `.qqcode/skills` | ưu tiên project `.qqcode`, dedupe theo tên |

Rollback: config `prompt_sections.enabled=false` dùng lại hàm concat cũ; templates/tệp context files độc lập có thể tắt.

## 8. Commit plan

```
refactor(prompt): build the system prompt from named sections
fix(prompt): keep cacheable sections stable when switching modes
feat(prompt): load context files from parent directories
feat(prompt): support parameterized prompt templates as commands
feat(skills): include skill locations in the prompt listing
feat(skills): invoke skills explicitly with slash commands
```
