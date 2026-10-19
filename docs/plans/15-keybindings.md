# Plan 15 — Customizable keybindings

> Gap: C10 · Mức độ: G2×1 · Wave 2

## 1. Mục tiêu

1. Người dùng đổi phím tắt qua file cấu hình, không sửa code: `~/.qqcode/keybindings.toml` (+ `.qqcode/keybindings.toml` project khi trust).
2. Mọi action của TUI có **tên ổn định** và binding mặc định, hiển thị trong `/hotkeys`.
3. Phát hiện xung đột/key không hợp lệ và báo rõ, không làm hỏng TUI.
4. Reload nóng khi file đổi (`watchfiles` đã có) — giống `/reload` config.
5. Giữ nguyên binding mặc định hiện tại nếu người dùng không cấu hình.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Binding hard-code trong class | `vibe/cli/textual_ui/app.py:129-136` (`BINDINGS`) |
| Không có khái niệm action configurable | `app.py:129-136` |
| Chỉ có `vim_keybindings: bool` trong config | `vibe/core/config.py:839` |
| Không có `/hotkeys` | `vibe/cli/commands.py:18-70` |
| Reload config đã có cơ chế | `app.py:_reload_config` (`:838`), `watchfiles` dependency |

## 3. Thiết kế

### 3.1 Model cấu hình (`vibe/core/config.py`)

```python
class KeybindingsConfig(BaseModel):
    enabled: bool = True
    path: Path | None = None                       # override vị trí file
    bindings: dict[str, str | list[str]] = {}      # action -> "ctrl+o" | ["ctrl+o", "f2"]

    @model_validator(mode="after")
    def _validate_keys(self) -> "KeybindingsConfig":
        """Dùng chính parser của Textual để validate; lỗi -> ValueError rõ ràng."""
```

- File TOML dạng phẳng:
  ```toml
  [bindings]
  toggle_tool = "ctrl+o"
  cycle_model = ["ctrl+p", "f3"]
  interrupt = "escape"
  ```
- Thứ tự ưu tiên: default (code) < global file < project file < `config.toml` inline. Env `QQCODE_KEYBINDINGS` override path.

### 3.2 Action registry (`vibe/cli/keybindings.py` mới)

```python
@dataclass(frozen=True)
class KeyAction:
    name: str                 # "toggle_tool"
    description: str          # "Toggle tool output"
    default: list[str]        # ["ctrl+o"]
    priority: bool = False

KEY_ACTIONS: tuple[KeyAction, ...] = (...)
```

- Mỗi action ánh xạ tới một method/`action_*` hiện có của `App` (hoặc command dispatch). Registry là nguồn sự thật cho cả `BINDINGS` lẫn `/hotkeys`.

### 3.3 Áp dụng vào TUI (`app.py`)

```python
def build_bindings(actions, overrides) -> list[Binding]:
    """Sinh BINDINGS động từ registry + override; phát hiện trùng phím (last wins +
    warning), log action bị bỏ. Không raise khi chỉ có cảnh báo."""

class QQCodeApp(App):
    BINDINGS = []           # thay bằng gán trong on_mount/__init__
    def _apply_keybindings(self) -> None:
        self._bindings = build_bindings(KEY_ACTIONS, self.config.keybindings)
        ...
```

- Binding không hợp lệ (sai cú pháp) → giữ mặc định cho action đó + hiển thị toast cảnh báo.
- Trùng phím giữa hai action → action sau thắng, log + cảnh báo trong `/hotkeys`.

### 3.4 `/hotkeys` command

| | |
|---|---|
| `/hotkeys` | in bảng action → phím hiện tại → mô tả; đánh dấu action đã override |
| `/hotkeys edit` | mở đường dẫn file keybindings (hoặc tạo template) |

- Đăng ký trong `CommandRegistry` (plan 07 đã làm registry động).

### 3.5 Reload nóng

- `watchfiles` đang theo dõi config; mở rộng theo dõi `keybindings.toml`; khi đổi → rebuild bindings, không reset session.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 15.1 | `KeybindingsConfig` + validator (dùng parser Textual) | `vibe/core/config.py` | S | — |
| 15.2 | `KeyAction` registry + defaults khớp `BINDINGS` hiện tại | `vibe/cli/keybindings.py` (mới) | M | — |
| 15.3 | `build_bindings` + áp động vào `App` | `app.py` | M | 15.2 |
| 15.4 | Load file global/project + merge ưu tiên | `keybindings.py`, `config.py` | M | 15.1 |
| 15.5 | `/hotkeys` (+ `edit`) | `commands.py`, `app.py` | S | 15.3 |
| 15.6 | Reload nóng qua `watchfiles` | `app.py` | S | 15.4 |
| 15.7 | Cảnh báo xung đột/không hợp lệ | `keybindings.py`, `app.py` | S | 15.3 |
| 15.8 | Docs `docs/keybindings.md` + template file | docs | S | 15.5 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/cli/test_keybindings_load.py` | file TOML hợp lệ → override đúng; sai cú pháp → giữ default + warning |
| `tests/cli/test_keybindings_merge.py` | thứ tự ưu tiên default < global < project < inline |
| `tests/cli/test_keybindings_conflict.py` | trùng phím → last wins + cảnh báo; không crash |
| `tests/cli/test_hotkeys_command.py` | `/hotkeys` liệt kê action + đánh dấu override |
| `tests/test_config_migration.py` | config cũ không có `[keybindings]` → giữ binding mặc định |

## 6. DoD

- [x] Đổi `toggle_tool` trong `keybindings.toml` có hiệu lực sau reload, không sửa code.
- [x] `/hotkeys` hiển thị đúng phím mặc định khi không cấu hình.
- [x] Xung đột/sai cú pháp chỉ cảnh báo, TUI vẫn dùng được.
- [x] Config cũ không có `[keybindings]` giữ nguyên hành vi binding hiện tại.
- [x] `ruff`, `pyright`, `pytest` xanh.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Override phá hành vi Textual built-in (escape/ctrl+c) | đánh dấu action `priority`/không cho override các phím thoát bắt buộc |
| Parser phím Textual đổi giữa version | dùng chính `Binding`/parser của Textual để validate; test |
| Reload làm mất focus/state | chỉ rebuild bindings, không remount màn hình; test snapshot |

Rollback: `keybindings.enabled=false` → dùng binding mặc định trong code; file bị bỏ qua.

## 8. Commit plan

```
feat(cli): load keybindings from a config file
feat(cli): build tui bindings from a named action registry
feat(cli): list and edit hotkeys from the command palette
fix(cli): warn instead of failing on keybinding conflicts
```
